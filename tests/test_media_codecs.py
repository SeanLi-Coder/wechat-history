"""Exercise local media codecs using generated pictures, tones, and videos."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from PIL import Image, TiffImagePlugin

import vault.media_codecs as codecs
from vault.media_codecs import (ImageKey, MediaCodecError, convert_audio, decode_image,
                                discover_image_keys, prepare_video)


def image_bytes(format="PNG"):
    picture = Image.new("RGB", (32, 24))
    picture.putdata([(x * 7 % 256, x * 13 % 256, x * 23 % 256) for x in range(32 * 24)])
    stream = io.BytesIO()
    picture.save(stream, format=format)
    return stream.getvalue()


def mpo_bytes(sizes=((32, 24), (24, 16), (16, 12))):
    pictures = []
    for index, size in enumerate(sizes):
        stream = io.BytesIO()
        Image.new("RGB", size, (30 + index * 40, 60, 90)).save(stream, format="JPEG")
        pictures.append(stream.getvalue())

    def header(entries):
        directory = TiffImagePlugin.ImageFileDirectory_v2()
        directory[0xB000] = b"0100"
        directory[0xB001] = len(pictures)
        directory[0xB002] = entries
        tiff = b"II*\x00" + struct.pack("<I", 8) + directory.tobytes(8)
        return b"\xff\xe2" + struct.pack(">H", len(tiff) + 6) + b"MPF\x00" + tiff

    marker_size = len(header(bytes(16 * len(pictures))))
    entries, offset = [], 0
    for index, picture in enumerate(pictures):
        size = len(picture) + (marker_size if index == 0 else 0)
        entries.append(struct.pack("<LLLHH", 0x030000 if index == 0 else 0, size,
                                   0 if index == 0 else offset - 10, 0, 0))
        offset += size
    return pictures[0][:2] + header(b"".join(entries)) + pictures[0][2:] + b"".join(pictures[1:])


def dat_image(raw, key, magic=codecs.V2_MAGIC, aes_size=64, xor_size=20):
    padder = PKCS7(128).padder()
    prefix = padder.update(raw[:aes_size]) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.aes_key), modes.ECB()).encryptor()
    encrypted = encryptor.update(prefix) + encryptor.finalize()
    return (magic + struct.pack("<II", aes_size, xor_size) + b"\x00" + encrypted
            + raw[aes_size:-xor_size] + bytes(value ^ key.xor_key for value in raw[-xor_size:]))


class MediaCodecTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.dat"
        self.destination = self.root / "output" / "media.bin"
        self.key = ImageKey(b"0123456789abcdef", 0x6A)

    def assert_no_output(self):
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob(".shiliao-media-*")), [])

    def test_plain_images_are_preserved_exactly_with_private_output(self):
        for format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            with self.subTest(format=format):
                raw = image_bytes(format)
                self.source.write_bytes(raw)
                self.destination.unlink(missing_ok=True)
                info = decode_image(self.source, self.destination)
                self.assertEqual(self.destination.read_bytes(), raw)
                self.assertEqual(info["mime_type"], mime)
                self.assertEqual((info["width"], info["height"]), (32, 24))
                self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
                self.assertFalse(info["first_frame_only"])

    def test_legacy_xor_is_detected_and_decoded(self):
        raw = image_bytes()
        self.source.write_bytes(bytes(value ^ 0xAF for value in raw))
        info = decode_image(self.source, self.destination)
        self.assertEqual(info["decoder"], "legacy_xor")
        self.assertEqual(self.destination.read_bytes(), raw)

    def test_translate_xor_matches_every_byte_and_key(self):
        raw = bytes(range(256)) * 2
        for key in range(256):
            self.assertEqual(codecs._xor(raw, key), bytes(value ^ key for value in raw))

    def test_standard_image_does_not_create_optional_original(self):
        raw = image_bytes()
        self.source.write_bytes(raw)
        original = self.root / "original.wxgf"
        info = decode_image(self.source, self.destination, original_destination=original)
        self.assertEqual(self.destination.read_bytes(), raw)
        self.assertFalse(info["original_saved"])
        self.assertFalse(original.exists())

    def test_mpo_preserves_every_original_byte_and_marks_first_picture_display(self):
        raw = mpo_bytes()
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                self.source.write_bytes(dat_image(raw, self.key) if encrypted else raw)
                self.destination.unlink(missing_ok=True)
                info = decode_image(self.source, self.destination, self.key)
                self.assertEqual(self.destination.read_bytes(), raw)
                self.assertEqual(info["format"], "jpg")
                self.assertEqual(info["mime_type"], "image/jpeg")
                self.assertEqual(info["original_format"], "mpo")
                self.assertEqual(info["frames"], 3)
                self.assertEqual((info["width"], info["height"]), (32, 24))
                self.assertTrue(info["first_frame_only"])
                self.assertTrue(info["playable"])
                with Image.open(self.destination) as picture:
                    self.assertEqual(picture.format, "MPO")
                    for index in range(picture.n_frames):
                        picture.seek(index)
                        picture.load()

    def test_mpo_with_truncated_later_frame_is_not_published(self):
        raw = mpo_bytes(((32, 24), (24, 16)))
        second = raw.find(b"\xff\xd8\xff", 3)
        self.assertGreater(second, 0)
        self.source.write_bytes(raw[:second + 120])
        with self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "invalid_image")
        self.assert_no_output()

    def test_malformed_mpo_metadata_cannot_fall_back_to_a_single_jpeg(self):
        raw = bytearray(mpo_bytes())
        tiff_header = raw.index(b"MPF\x00") + 4
        raw[tiff_header + 2:tiff_header + 4] = b"\x00\x00"
        self.source.write_bytes(raw)
        with self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "invalid_image")
        self.assert_no_output()

    def test_mpo_later_frame_obeys_individual_pixel_limit(self):
        self.source.write_bytes(mpo_bytes(((8, 8), (64, 48))))
        with patch.object(codecs, "MAX_IMAGE_PIXELS", 2048), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "invalid_image")
        self.assert_no_output()

    def test_mpo_aggregate_limit_counts_actual_frame_dimensions(self):
        self.source.write_bytes(mpo_bytes(((8, 8), (32, 24))))
        with patch.object(codecs, "MAX_ANIMATION_PIXELS", 800), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "invalid_image")
        self.assert_no_output()

    def test_v2_roundtrip_preserves_encrypted_plain_and_xor_segments(self):
        raw = image_bytes()
        self.source.write_bytes(dat_image(raw, self.key))
        before = self.source.read_bytes()
        info = decode_image(self.source, self.destination, self.key)
        self.assertEqual(info["decoder"], "v2")
        self.assertEqual(self.destination.read_bytes(), raw)
        self.assertEqual(self.source.read_bytes(), before)

    def test_v2_key_candidates_are_validated_against_whole_image(self):
        raw = image_bytes()
        self.source.write_bytes(dat_image(raw, self.key))
        wrong_xor = ImageKey(self.key.aes_key, self.key.xor_key ^ 1)
        decode_image(self.source, self.destination, keys=[wrong_xor, self.key])
        self.assertEqual(self.destination.read_bytes(), raw)

    def test_v1_fixed_aes_and_derived_xor_are_supported(self):
        raw = image_bytes("JPEG")
        key = ImageKey(b"cfcd208495d565ef", 0x77)
        self.source.write_bytes(dat_image(raw, key, magic=codecs.V1_MAGIC))
        info = decode_image(self.source, self.destination)
        self.assertEqual(info["decoder"], "v1")
        self.assertEqual(self.destination.read_bytes(), raw)

    def test_wrong_key_and_malformed_lengths_leave_no_output_or_secret(self):
        raw = image_bytes()
        for data, key in (
            (dat_image(raw, self.key), ImageKey(b"fedcba9876543210", 0x6A)),
            (codecs.V2_MAGIC + struct.pack("<II", 2**32 - 1, 20) + b"\0", self.key),
        ):
            with self.subTest(length=len(data)):
                self.source.write_bytes(data)
                capture = io.StringIO()
                with redirect_stdout(capture), redirect_stderr(capture), self.assertRaises(MediaCodecError) as error:
                    decode_image(self.source, self.destination, key)
                self.assertEqual(error.exception.code, "invalid_image")
                self.assertNotIn(key.aes_key.decode(), str(error.exception))
                self.assertEqual(capture.getvalue(), "")
                self.assert_no_output()
        self.assertNotIn(self.key.aes_key.decode(), repr(self.key))

    def test_v2_without_key_reports_missing_key(self):
        self.source.write_bytes(dat_image(image_bytes(), self.key))
        with self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "image_key_missing")
        self.assert_no_output()

    def test_corrupted_image_is_not_published(self):
        raw = bytearray(image_bytes())
        raw[len(raw) // 2] ^= 1
        self.source.write_bytes(raw)
        with self.assertRaises(MediaCodecError):
            decode_image(self.source, self.destination)
        self.assert_no_output()

    def test_source_change_is_detected_before_publication(self):
        self.source.write_bytes(image_bytes())
        original = codecs._image_info

        def mutate(data):
            info = original(data)
            self.source.write_bytes(image_bytes("JPEG"))
            return info

        with patch.object(codecs, "_image_info", side_effect=mutate), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "source_changed")
        self.assert_no_output()

    def test_destination_and_source_symlinks_are_not_followed(self):
        self.source.write_bytes(image_bytes())
        link = self.root / "link.dat"
        link.symlink_to(self.source)
        with self.assertRaises(MediaCodecError) as error:
            decode_image(link, self.destination)
        self.assertEqual(error.exception.code, "unsafe_source")
        self.destination.parent.mkdir(exist_ok=True)
        self.destination.symlink_to(self.source)
        before = self.source.read_bytes()
        with self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination)
        self.assertEqual(error.exception.code, "destination_exists")
        self.assertEqual(self.source.read_bytes(), before)

    def test_mac_keys_are_derived_only_from_local_metadata(self):
        cache = self.root / "app_data/net/kvcomm"
        cache.mkdir(parents=True)
        (cache / "key_12345678_0.statistic").touch()
        (cache / "key_reportnow_12345678.statistic").touch()
        account = self.root / "wxid_synthetic_abcd"
        capture = io.StringIO()
        with redirect_stdout(capture), redirect_stderr(capture):
            keys = discover_image_keys(account / "db_storage", self.root / "app_data")
        expected = hashlib.md5(b"12345678wxid_synthetic").hexdigest()[:16].encode()
        self.assertTrue(any(key.aes_key == expected and key.xor_key == (12345678 & 255) for key in keys))
        self.assertEqual(capture.getvalue(), "")
        self.assertNotIn(expected.decode(), repr(keys))

    def make_wave(self):
        samples = b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * index / 24000)))
                           for index in range(4800))
        with wave.open(str(self.source), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(samples)
        return samples

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_flac_compression_preserves_all_pcm_samples(self):
        samples = self.make_wave()
        info = convert_audio(self.source, self.destination)
        self.assertEqual(info["mime_type"], "audio/flac")
        self.assertTrue(info["lossless"])
        result = subprocess.run([codecs._tool("ffmpeg"), "-v", "error", "-i", str(self.destination),
                                 "-f", "s16le", "-"], capture_output=True, check=True)
        self.assertEqual(result.stdout, samples)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_real_tencent_silk_decodes_to_lossless_flac(self):
        try:
            import pilk
        except ImportError:
            self.skipTest("The pilk decoder is not installed")
        samples = self.make_wave()
        pcm = self.root / "synthetic.pcm"
        pcm.write_bytes(samples)
        pilk.encode(str(pcm), str(self.source), pcm_rate=24000, tencent=True)
        before = self.source.read_bytes()
        info = convert_audio(self.source, self.destination)
        self.assertTrue(info["source_silk"])
        self.assertTrue(info["lossless"])
        self.assertAlmostEqual(info["duration"], .2, places=2)
        self.assertEqual(self.source.read_bytes(), before)
        expected = self.root / "expected.pcm"
        pilk.decode(str(self.source), str(expected), pcm_rate=24000)
        result = subprocess.run([codecs._tool("ffmpeg"), "-v", "error", "-i", str(self.destination),
                                 "-f", "s16le", "-"], capture_output=True, check=True)
        self.assertEqual(result.stdout, expected.read_bytes())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_video_is_remuxed_without_reencoding(self):
        subprocess.run([codecs._tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=red:size=32x24:rate=10",
                        "-t", "0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4", "-y", str(self.source)],
                       capture_output=True, check=True)
        before = hashlib.sha256(self.source.read_bytes()).digest()
        info = prepare_video(self.source, self.destination)
        self.assertEqual(info["codec"], "h264")
        self.assertTrue(info["lossless"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)
        checksums = []
        for path in (self.source, self.destination):
            result = subprocess.run([codecs._tool("ffmpeg"), "-v", "error", "-i", str(path),
                                     "-map", "0:v:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"],
                                    capture_output=True, check=True)
            checksums.append(result.stdout)
        self.assertEqual(checksums[0], checksums[1])

    def test_playlist_and_unsafe_audio_are_rejected(self):
        self.source.write_bytes(b"#EXTM3U\nhttps://example.com/audio.mp3\n")
        with patch.object(codecs, "_run") as run, self.assertRaises(MediaCodecError) as error:
            convert_audio(self.source, self.destination)
        self.assertEqual(error.exception.code, "unsupported_audio")
        run.assert_not_called()
        self.assert_no_output()

    def test_decoder_failures_do_not_echo_captured_diagnostics(self):
        self.make_wave()
        failure = subprocess.TimeoutExpired("private decoder", 1, output=b"private media metadata")
        with patch.object(codecs.subprocess, "run", side_effect=failure), self.assertRaises(MediaCodecError) as error:
            convert_audio(self.source, self.destination)
        self.assertEqual(error.exception.code, "conversion_failed")
        self.assertNotIn("private media", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assert_no_output()

    def make_wxgf(self):
        bitstream = self.root / "synthetic.hevc"
        subprocess.run([codecs._tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=blue:size=80x64",
                        "-frames:v", "1", "-c:v", "libx265", "-x265-params", "log-level=error:pools=1",
                        "-f", "hevc", "-y", str(bitstream)], capture_output=True, check=True)
        raw = b"wxgf" + bytes(12) + bitstream.read_bytes()
        self.source.write_bytes(raw)
        return raw

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_wxgf_preview_is_marked_and_original_can_be_preserved(self):
        raw = self.make_wxgf()
        preview = decode_image(self.source, self.destination)
        self.assertEqual(preview["format"], "png")
        self.assertEqual(preview["original_format"], "wxgf")
        self.assertTrue(preview["first_frame_only"])
        self.assertTrue(preview["playable"])
        original = self.root / "original.wxgf"
        preserved = decode_image(self.source, original, preserve_wxgf=True)
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(preserved["format"], "wxgf")
        self.assertFalse(preserved["playable"])
        self.assertFalse(preserved["first_frame_only"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_wxgf_preview_and_decrypted_original_use_one_decode(self):
        raw = self.make_wxgf()
        encrypted = dat_image(raw, self.key)
        self.source.write_bytes(encrypted)
        original = self.root / "original.wxgf"
        with patch.object(codecs, "_wxgf_image", wraps=codecs._wxgf_image) as decode:
            info = decode_image(self.source, self.destination, self.key, original_destination=original)
        self.assertEqual(decode.call_count, 1)
        self.assertTrue(info["original_saved"])
        self.assertTrue(info["first_frame_only"])
        self.assertTrue(info["playable"])
        self.assertEqual(info["original_format"], "wxgf")
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(original.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.read_bytes(), encrypted)
        self.assertEqual(codecs._image_info(self.destination.read_bytes())["format"], "png")

    def mock_wxgf_preview(self, data, output):
        raw = image_bytes()
        output.write_bytes(raw)
        return {**codecs._image_info(raw), "first_frame_only": True}

    def test_wxgf_companion_write_failure_publishes_neither_file(self):
        self.source.write_bytes(b"wxgf synthetic fixture")
        original = self.root / "original.wxgf"
        write_bytes = Path.write_bytes

        def fail_original(path, data):
            if path.name == "original.bin":
                raise OSError("private filesystem details")
            return write_bytes(path, data)

        with patch.object(codecs, "_wxgf_image", side_effect=self.mock_wxgf_preview), \
                patch.object(Path, "write_bytes", fail_original), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination, original_destination=original)
        self.assertEqual(error.exception.code, "media_io_failed")
        self.assertNotIn("private", str(error.exception))
        self.assertFalse(original.exists())
        self.assert_no_output()

    def test_wxgf_second_publication_failure_rolls_back_preview(self):
        self.source.write_bytes(b"wxgf synthetic fixture")
        original = self.root / "original.wxgf"
        link = codecs.os.link

        def race_original(source, destination):
            if destination == original:
                original.write_bytes(b"existing unrelated file")
            link(source, destination)

        with patch.object(codecs, "_wxgf_image", side_effect=self.mock_wxgf_preview), \
                patch.object(codecs.os, "link", side_effect=race_original), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination, original_destination=original)
        self.assertEqual(error.exception.code, "destination_exists")
        self.assertEqual(original.read_bytes(), b"existing unrelated file")
        self.assert_no_output()

    def test_wxgf_source_change_publishes_neither_file(self):
        self.source.write_bytes(b"wxgf synthetic fixture")
        original = self.root / "original.wxgf"

        def mutate(data, output):
            info = self.mock_wxgf_preview(data, output)
            self.source.write_bytes(b"wxgf changed fixture")
            return info

        with patch.object(codecs, "_wxgf_image", side_effect=mutate), self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination, original_destination=original)
        self.assertEqual(error.exception.code, "source_changed")
        self.assertFalse(original.exists())
        self.assert_no_output()

    def test_original_destination_never_overwrites_source_or_existing_file(self):
        raw = b"wxgf synthetic fixture"
        self.source.write_bytes(raw)
        existing = self.root / "existing.wxgf"
        existing.write_bytes(b"unrelated original")
        link = self.root / "link.wxgf"
        link.symlink_to(self.source)
        for original in (self.source, self.destination, existing, link):
            with self.subTest(destination=original.name):
                with patch.object(codecs, "_wxgf_image") as decode, self.assertRaises(MediaCodecError) as error:
                    decode_image(self.source, self.destination, original_destination=original)
                self.assertEqual(error.exception.code, "destination_exists")
                decode.assert_not_called()
                self.assert_no_output()
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertEqual(existing.read_bytes(), b"unrelated original")

    def test_original_only_and_companion_modes_are_mutually_exclusive(self):
        self.source.write_bytes(b"wxgf synthetic fixture")
        with self.assertRaises(MediaCodecError) as error:
            decode_image(self.source, self.destination, preserve_wxgf=True,
                         original_destination=self.root / "original.wxgf")
        self.assertEqual(error.exception.code, "invalid_destination")
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()

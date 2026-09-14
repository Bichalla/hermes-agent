import tempfile
import unittest
from unittest import mock
from pathlib import Path

from bridge.evidence import SourceReader, verify_snapshot


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.reader = SourceReader(self.root, [self.root])

    def tearDown(self):
        self.tmp.cleanup()

    def test_script_is_read_without_import_or_execution(self):
        script = self.root / 'preflight.py'
        script.write_text("raise AssertionError('MUST NOT EXECUTE')\n")
        result = self.reader.read({'path': 'preflight.py'})
        self.assertIn('MUST NOT EXECUTE', result['text'])
        self.assertTrue(verify_snapshot(self.reader.snapshot()))
        script.write_text('changed')
        self.assertFalse(verify_snapshot(self.reader.snapshot()))

    def test_outside_private_symlink_and_non_source_are_not_read(self):
        (self.root / 'data.db').write_text('private')
        (self.root / '.env').write_text('private')
        (self.root / 'source.py').symlink_to('/etc/passwd')
        for path in ['../outside.py', '.env', 'data.db', 'source.py']:
            with self.subTest(path=path):
                self.assertIn('error', self.reader.read({'path': path}))

    def test_long_file_returns_line_index_and_bounded_pages(self):
        (self.root / 'module.py').write_text('x = 1\n' * 1000 + 'def important():\n    return 2\n')
        result = self.reader.read({'path': 'module.py', 'start_line': 995, 'max_lines': 10})
        self.assertTrue(result['partial'])
        self.assertIn('def important', result['text'])
        self.assertEqual(result['total_lines'], 1002)

    def test_redaction_and_raw_content_integrity_are_separate(self):
        (self.root / 'sample.py').write_text("API_KEY = 'sk-proj-" + 'a' * 80 + "'\n")
        result = self.reader.read({'path': 'sample.py'})
        self.assertNotIn('a' * 80, result['text'])
        self.assertTrue(verify_snapshot(self.reader.snapshot()))

    def test_source_budget_includes_import_time_excerpts(self):
        (self.root / 'large.py').write_text('x = 1\n' + "print('" + 'a' * 500 + "')\n")
        with mock.patch('bridge.evidence.MAX_TOTAL_CHARS', 300):
            result = self.reader.read({'path': 'large.py', 'max_lines': 1})
        self.assertEqual(result['error'], 'source_text_budget_exhausted')

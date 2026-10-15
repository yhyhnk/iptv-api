import errno
import unittest
from unittest.mock import patch

from utils.docker_compat import enable_positional_write_compatibility


class DockerCompatibilityTests(unittest.TestCase):
    def test_healthy_write_does_not_install_a_filter(self):
        with patch('utils.docker_compat.sys.platform', 'linux'), patch(
            'utils.docker_compat.os.pwrite', return_value=5
        ), patch('utils.docker_compat._install_write_fallback') as install:
            self.assertFalse(enable_positional_write_compatibility())
        install.assert_not_called()

    def test_probe_io_error_does_not_block_startup_or_install_filter(self):
        with patch('utils.docker_compat.sys.platform', 'linux'), patch(
            'utils.docker_compat.os.pwrite', side_effect=OSError(errno.EIO, 'disk I/O error')
        ), patch('utils.docker_compat._install_write_fallback') as install:
            self.assertFalse(enable_positional_write_compatibility())
        install.assert_not_called()

    def test_unavailable_temporary_directory_does_not_block_startup(self):
        with patch('utils.docker_compat.sys.platform', 'linux'), patch(
            'utils.docker_compat.tempfile.TemporaryFile',
            side_effect=PermissionError(errno.EACCES, 'temporary directory unavailable')
        ), patch('utils.docker_compat._install_write_fallback') as install:
            self.assertFalse(enable_positional_write_compatibility())
        install.assert_not_called()

    def test_persistent_permission_error_is_not_hidden(self):
        with patch('utils.docker_compat.sys.platform', 'linux'), patch(
            'utils.docker_compat.os.pwrite', side_effect=PermissionError(errno.EPERM, 'denied')
        ), patch('utils.docker_compat._install_write_fallback'):
            with self.assertRaises(PermissionError):
                enable_positional_write_compatibility()

    def test_non_linux_process_does_not_probe_or_install(self):
        with patch('utils.docker_compat.sys.platform', 'win32'), patch(
            'utils.docker_compat.os.pwrite', create=True
        ) as write, patch('utils.docker_compat._install_write_fallback') as install:
            self.assertFalse(enable_positional_write_compatibility())
        write.assert_not_called()
        install.assert_not_called()


if __name__ == '__main__':
    unittest.main()

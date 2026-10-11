import cv2
import numpy as np
import pytest

from barcode_system.camera import ImageFolderCamera, open_camera, resolve_backend


@pytest.mark.parametrize("platform,expected", [
    ("linux", ("v4l2", cv2.CAP_V4L2)),
    ("win32", ("dshow", cv2.CAP_DSHOW)),
    ("darwin", ("any", cv2.CAP_ANY)),
])
def test_auto_backend_by_os(platform, expected):
    assert resolve_backend("auto", platform) == expected


def test_explicit_backend_overrides_os():
    assert resolve_backend("msmf", "linux") == ("msmf", cv2.CAP_MSMF)


def test_unknown_backend():
    with pytest.raises(ValueError):
        resolve_backend("gstreamer")


def test_image_folder_camera_cycles(tmp_path):
    for i, v in enumerate((10, 20)):
        cv2.imwrite(str(tmp_path / f"{i}.png"), np.full((4, 4), v, np.uint8))
    (tmp_path / "notes.txt").write_text("ignored")
    cam = ImageFolderCamera(tmp_path, fps=0)
    values = [int(cam.read()[0, 0]) for _ in range(5)]
    assert values == [10, 20, 10, 20, 10]


def test_image_folder_camera_empty(tmp_path):
    with pytest.raises(RuntimeError):
        ImageFolderCamera(tmp_path)


def test_open_camera_files_requires_folder():
    with pytest.raises(RuntimeError):
        open_camera("files", images=None)

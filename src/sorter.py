import os
import shutil
from pathlib import Path


def sort_image(image_path: str, expression: str, output_dir: str, mode: str = "copy") -> str:
    """Copy, move or link an image into its expression's folder, and say where it went."""
    src = Path(image_path)
    dest_dir = Path(output_dir, expression)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    # Avoid overwriting by appending a counter. lexists, so a link whose
    # image has gone still counts as taken.
    counter = 1
    while os.path.lexists(dest):
        dest = dest_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1

    if mode == "move":
        shutil.move(src, dest)
    elif mode == "link":
        os.symlink(host_path(str(src.absolute())), dest)
    else:
        shutil.copy2(src, dest)
    return str(dest)


def host_path(path: str) -> str:
    """Where a file is outside the Flatpak sandbox, so links to it work there.

    Folders picked outside Pictures reach the app through the document portal,
    at a /run/user/… path that stands in for the real one.
    """
    try:
        return os.getxattr(path, "user.document-portal.host-path").decode().rstrip("\0")
    except OSError:
        return path

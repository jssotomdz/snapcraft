# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2022 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Copy assets to their final locations."""

import itertools
import os
import shutil
import stat
import textwrap
import urllib.parse
from pathlib import Path
from typing import List, Optional

import requests
from craft_cli import emit

from snapcraft import errors
from snapcraft.projects import Project

from .desktop_file import DesktopFile


def setup_assets(project: Project, *, assets_dir: Path, prime_dir: Path) -> None:
    """Copy gui assets to the appropriate location in the snap filesystem.

    :param project: The snap project file.
    :param assets_dir: The directory containing snap project assets.
    :param prime_dir: The directory containing the content to be snapped.
    """
    meta_dir = prime_dir / "meta"
    gui_dir = meta_dir / "gui"
    gui_dir.mkdir(parents=True, exist_ok=True)

    _write_snap_directory(assets_dir=assets_dir, prime_dir=prime_dir, meta_dir=meta_dir)
    _write_snapcraft_runner(prime_dir=prime_dir)
    # TODO: write snapcraft

    if not project.apps:
        return

    icon_path = _finalize_icon(
        project.icon, assets_dir=assets_dir, gui_dir=gui_dir, prime_dir=prime_dir
    )
    relative_icon_path: Optional[str] = None

    if icon_path is not None:
        if prime_dir in icon_path.parents:
            icon_path = icon_path.relative_to(prime_dir)
        relative_icon_path = str(icon_path)

    for app_name, app in project.apps.items():
        if not app.desktop:
            continue

        desktop_file = DesktopFile(
            snap_name=project.name,
            app_name=app_name,
            filename=app.desktop,
            prime_dir=prime_dir,
        )
        desktop_file.write(gui_dir=gui_dir, icon_path=relative_icon_path)

        _validate_command_chain(
            app.command_chain, app_name=app_name, prime_dir=prime_dir
        )

    # TODO: copy gadget and kernel assets


def _finalize_icon(
    icon: Optional[str], *, assets_dir: Path, gui_dir: Path, prime_dir: Path
) -> Optional[Path]:
    """Ensure sure icon is properly configured and installed.

    Fetch from a remote URL, if required, and place in the meta/gui
    directory.
    """
    # Nothing to do if no icon is configured, search for existing icon.
    if icon is None:
        return _find_icon_file(assets_dir)

    # Extracted appstream icon paths will either:
    # (1) point to a file relative to prime
    # (2) point to a remote http(s) url
    #
    # The 'icon' specified in the snapcraft.yaml has the same
    # constraint as (2) and would have already been validated
    # as existing by the schema.  So we can treat it the same
    # at this point, regardless of the source of the icon.
    parsed_url = urllib.parse.urlparse(icon)
    parsed_path = Path(parsed_url.path)
    icon_ext = parsed_path.suffix[1:]
    target_icon_path = Path(gui_dir, f"icon.{icon_ext}")

    target_icon_path.parent.mkdir(parents=True, exist_ok=True)
    if parsed_url.scheme in ["http", "https"]:
        # Remote - fetch URL and write to target.
        emit.progress(f"Fetching icon from {icon!r}")
        icon_data = requests.get(icon).content
        target_icon_path.write_bytes(icon_data)
    elif parsed_url.scheme == "":
        source_path = Path(
            prime_dir,
            parsed_path.relative_to("/") if parsed_path.is_absolute() else parsed_path,
        )
        if source_path.exists():
            # Local with path relative to prime.
            shutil.copy(source_path, target_icon_path)
        elif parsed_path.exists():
            # Local with path relative to project.
            shutil.copy(parsed_path, target_icon_path)
        else:
            # No icon found, fall back to searching for existing icon.
            return _find_icon_file(assets_dir)
    else:
        raise RuntimeError(f"Unexpected icon path: {parsed_url!r}")

    return target_icon_path


def _find_icon_file(assets_dir: Path) -> Optional[Path]:
    for icon_path in (assets_dir / "gui/icon.png", assets_dir / "gui/icon.svg"):
        if icon_path.is_file():
            return icon_path
    return None


def _validate_command_chain(
    command_chain: List[str], *, app_name: str, prime_dir: Path
) -> None:
    """Verify if each item in the command chain is executble."""
    for item in command_chain:
        executable_path = prime_dir / item

        # command-chain entries must always be relative to the root of
        # the snap, i.e. PATH is not used.
        if not _is_executable(executable_path):
            raise errors.SnapcraftError(
                f"Failed to generate snap metadata: The command-chain item {item!r} "
                f"defined in the app {app_name!r} does not exist or is not executable.",
                resolution=f"Ensure that {item!r} is relative to the prime directory.",
            )


def _is_executable(path: Path) -> bool:
    """Verify if the given path corresponds to an executable file."""
    if not path.is_file():
        return False

    mode = path.stat().st_mode
    return bool(mode & stat.S_IXUSR or mode & stat.S_IXGRP or mode & stat.S_IXOTH)


def _write_snapcraft_runner(*, prime_dir: Path):
    content = textwrap.dedent(
        """#!/bin/sh
        export PATH="$SNAP/usr/sbin:$SNAP/usr/bin:$SNAP/sbin:$SNAP/bin:$PATH"
        export LD_LIBRARY_PATH="$SNAP_LIBRARY_PATH:$LD_LIBRARY_PATH"
        exec "$@"
        """
    )

    runner_path = prime_dir / "snap/command-chain/snapcraft-runner"
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    runner_path.write_text(content)
    runner_path.chmod(0o755)


def _write_snap_directory(*, assets_dir: Path, prime_dir: Path, meta_dir: Path) -> None:
    """Record manifest and copy assets found under the assets directory.

    These assets have priority over any code generated assets and include:
    - hooks
    - gui
    """
    prime_snap_dir = prime_dir / "snap"

    snap_dir_iter = itertools.product([prime_snap_dir], ["hooks", "gui"])
    meta_dir_iter = itertools.product([meta_dir], ["hooks", "gui"])

    for origin in itertools.chain(snap_dir_iter, meta_dir_iter):
        src_dir = assets_dir / origin[1]
        dst_dir = origin[0] / origin[1]

        if src_dir.is_dir():
            dst_dir.mkdir(parents=True, exist_ok=True)
            for asset in os.listdir(src_dir):
                source = src_dir / asset
                destination = dst_dir / asset

                destination.unlink(missing_ok=True)

                shutil.copy(source, destination, follow_symlinks=True)

                # Ensure that the hook is executable in meta/hooks, this is a moot
                # point considering the prior link_or_copy call, but is technically
                # correct and allows for this operation to take place only once.
                if origin[0] == meta_dir and origin[1] == "hooks":
                    destination.chmod(0o755)

    # TODO: record manifest and source snapcraft.yaml
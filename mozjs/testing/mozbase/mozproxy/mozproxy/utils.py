"""Utility functions for Raptor"""
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import

import subprocess
import time
import bz2
import gzip
import os
import signal
import sys
from six.moves.urllib.request import urlretrieve

try:
    import zstandard
except ImportError:
    zstandard = None
try:
    import lzma
except ImportError:
    lzma = None

from mozlog import get_proxy_logger
from mozprocess import ProcessHandler
from mozproxy import mozharness_dir


LOG = get_proxy_logger(component="mozproxy")

external_tools_path = os.environ.get("EXTERNALTOOLSPATH", None)
if external_tools_path is not None:
    # running in production via mozharness
    TOOLTOOL_PATH = os.path.join(external_tools_path, "tooltool.py")
else:
    # running locally via mach
    TOOLTOOL_PATH = os.path.join(mozharness_dir, "external_tools", "tooltool.py")


def transform_platform(str_to_transform, config_platform, config_processor=None):
    """Transform platform name i.e. 'mitmproxy-rel-bin-{platform}.manifest'

    transforms to 'mitmproxy-rel-bin-osx.manifest'.
    Also transform '{x64}' if needed for 64 bit / win 10
    """
    if "{platform}" not in str_to_transform and "{x64}" not in str_to_transform:
        return str_to_transform

    if "win" in config_platform:
        platform_id = "win"
    elif config_platform == "mac":
        platform_id = "osx"
    else:
        platform_id = "linux64"

    if "{platform}" in str_to_transform:
        str_to_transform = str_to_transform.replace("{platform}", platform_id)

    if "{x64}" in str_to_transform and config_processor is not None:
        if "x86_64" in config_processor:
            str_to_transform = str_to_transform.replace("{x64}", "_x64")
        else:
            str_to_transform = str_to_transform.replace("{x64}", "")

    return str_to_transform


def tooltool_download(manifest, run_local, raptor_dir):
    """Download a file from tooltool using the provided tooltool manifest"""

    def outputHandler(line):
        LOG.info(line)

    if run_local:
        command = [sys.executable, TOOLTOOL_PATH, "fetch", "-o", "-m", manifest]
    else:
        # we want to use the tooltool cache in production
        if os.environ.get("TOOLTOOLCACHE") is not None:
            _cache = os.environ["TOOLTOOLCACHE"]
        else:
            # XXX top level dir? really?
            # that gets run locally on any platform
            # when you call ./mach python-test
            _cache = "/builds/tooltool_cache"

        command = [
            sys.executable,
            TOOLTOOL_PATH,
            "fetch",
            "-o",
            "-m",
            manifest,
            "-c",
            _cache,
        ]

    proc = ProcessHandler(
        command, processOutputLine=outputHandler, storeOutput=False, cwd=raptor_dir
    )

    proc.run()

    try:
        proc.wait()
    except Exception:
        if proc.poll() is None:
            proc.kill(signal.SIGTERM)


def archive_type(path):
    filename, extension = os.path.splitext(path)
    filename, extension2 = os.path.splitext(filename)
    if extension2 != "":
        extension = extension2
    if extension == ".tar":
        return "tar"
    elif extension == ".zip":
        return "zip"
    return None


def extract_archive(path, dest_dir, typ):
    """Extract an archive to a destination directory."""

    # Resolve paths to absolute variants.
    path = os.path.abspath(path)
    dest_dir = os.path.abspath(dest_dir)
    suffix = os.path.splitext(path)[-1]

    # We pipe input to the decompressor program so that we can apply
    # custom decompressors that the program may not know about.
    if typ == "tar":
        if suffix == ".bz2":
            ifh = bz2.open(str(path), "rb")
        elif suffix == ".gz":
            ifh = gzip.open(str(path), "rb")
        elif suffix == ".xz":
            if not lzma:
                raise ValueError("lzma Python package not available")
            ifh = lzma.open(str(path), "rb")
        elif suffix == ".zst":
            if not zstandard:
                raise ValueError("zstandard Python package not available")
            dctx = zstandard.ZstdDecompressor()
            ifh = dctx.stream_reader(path.open("rb"))
        elif suffix == ".tar":
            ifh = path.open("rb")
        else:
            raise ValueError("unknown archive format for tar file: %s" % path)
        args = ["tar", "xf", "-"]
        pipe_stdin = True
    elif typ == "zip":
        # unzip from stdin has wonky behavior. We don't use a pipe for it.
        ifh = open(os.devnull, "rb")
        args = ["unzip", "-o", str(path)]
        pipe_stdin = False
    else:
        raise ValueError("unknown archive format: %s" % path)

    LOG.info("Extracting %s to %s using %r" % (path, dest_dir, args))
    t0 = time.time()
    with ifh:
        p = subprocess.Popen(args, cwd=str(dest_dir), bufsize=0, stdin=subprocess.PIPE)
        while True:
            if not pipe_stdin:
                break
            chunk = ifh.read(131072)
            if not chunk:
                break
            p.stdin.write(chunk)
        # make sure we wait for the command to finish
        p.communicate()

    if p.returncode:
        raise Exception("%r exited %d" % (args, p.returncode))
    LOG.info("%s extracted in %.3fs" % (path, time.time() - t0))


def download_file_from_url(url, local_dest, extract=False):
    """Receive a file in a URL and download it, i.e. for the hostutils tooltool manifest
    the url received would be formatted like this:
      config/tooltool-manifests/linux64/hostutils.manifest"""
    if os.path.exists(local_dest):
        LOG.info("file already exists at: %s" % local_dest)
        if not extract:
            return True
    else:
        LOG.info("downloading: %s to %s" % (url, local_dest))
        _file, _headers = urlretrieve(url, local_dest)

    if not extract:
        return os.path.exists(local_dest)

    typ = archive_type(local_dest)
    if typ is None:
        return False

    extract_archive(local_dest, os.path.dirname(local_dest), typ)
    return True
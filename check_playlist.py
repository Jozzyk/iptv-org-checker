#!/usr/bin/env python3
"""Check IPTV streams and build a static M3U without hosting the streams."""

import argparse
import ipaddress
import json
import random
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore
from urllib.error import URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import urlopen


SOURCE = "https://iptv-org.github.io/iptv/index.m3u"
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
EXTINF_SEPARATOR = re.compile(r',(?=(?:[^"]*"[^"]*")*[^"]*$)')
PRIVATE_PARAMS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "jwt",
    "key", "password", "passwd", "signature", "sig", "token", "username",
}
HEADER_OPTIONS = {
    "http-user-agent": "-user_agent",
    "http-referrer": "-referer",
    "http-referer": "-referer",
}
PUBLIC_PROTOCOLS = {"http", "https", "rtmp", "rtmps", "mmsh", "srt"}
QUALITY_SUFFIX = re.compile(r"@(?:uhd|fhd|hd|sd|4k|2160p|1080p|720p|480p)$")


@dataclass(frozen=True)
class Stream:
    lines: tuple[str, ...]
    url: str
    host: str
    options: tuple[str, ...]


def parse_playlist(raw):
    lines = iter(raw.splitlines())
    header = next(lines, "").lstrip("\ufeff").strip()
    if not header.startswith("#EXTM3U"):
        raise ValueError("Source is not an extended M3U")
    groups = defaultdict(list)
    counts = Counter()
    entry = []
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            entry = [line]
            continue
        if not entry or not line:
            continue
        if line.startswith("#"):
            entry.append(line)
            continue

        counts["source_entries"] += 1
        stream = parse_stream(tuple(entry), line)
        entry = []
        if stream is None:
            counts["skipped_urls"] += 1
            continue
        metadata, *display = EXTINF_SEPARATOR.split(stream.lines[0], maxsplit=1)
        attrs = dict(ATTR.findall(metadata))
        channel_id = QUALITY_SUFFIX.sub("", attrs.get("tvg-id", "").strip().casefold())
        group = attrs.get("group-title", "").strip().casefold()
        name = " ".join("".join(display).casefold().split())
        key = ("id", channel_id) if channel_id else ("name", group, name or stream.url)
        groups[key].append(stream)
    return header, groups, counts


def parse_stream(lines, url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in PUBLIC_PROTOCOLS or not host or parsed.username is not None
                or parsed.password is not None or parsed.port == 0):
            return None
        if ("." not in host and ":" not in host) or host.rstrip(".").endswith(
            (".localhost", ".internal", ".local")
        ):
            return None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            return None
        if any(key.casefold() in PRIVATE_PARAMS for key, _ in parse_qsl(parsed.query)):
            return None
    except ValueError:
        return None

    options = {}
    for line in lines[1:]:
        if not line.startswith("#EXTVLCOPT:"):
            continue
        key, _, value = line.removeprefix("#EXTVLCOPT:").partition("=")
        if key.casefold() in HEADER_OPTIONS and value:
            options[HEADER_OPTIONS[key.casefold()]] = value
    # Keep only recognized player options so arbitrary directives aren't republished.
    safe_lines = [lines[0]]
    for key, flag in (("http-user-agent", "-user_agent"), ("http-referrer", "-referer")):
        if flag in options:
            safe_lines.append(f"#EXTVLCOPT:{key}={options[flag]}")
    args = tuple(value for pair in options.items() for value in pair)
    return Stream(tuple(safe_lines), url, host, args)


def probe(stream, timeout):
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist",
             "http,https,tls,tcp,udp,crypto,rtmp,rtmps,mmsh,srt",
             "-rw_timeout", "6500000", "-probesize", "524288",
             "-analyzeduration", "3000000", *stream.options,
             "-read_intervals", "%+#16", "-show_packets",
             "-show_entries", "packet=codec_type,size,flags", "-of", "json",
             "-i", stream.url],
            capture_output=True, timeout=timeout, check=False,
        )
        if result.returncode:
            return None
        packets = json.loads(result.stdout).get("packets", [])
        if any(
            packet.get("codec_type") in {"video", "audio"}
            and int(packet.get("size", 0)) > 0 and "C" not in packet.get("flags", "")
            for packet in packets
        ):
            return time.monotonic() - started
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return None


def check(groups, workers, timeout):
    targets = {
        (stream.url, stream.options): stream
        for streams in groups.values() for stream in streams
    }
    queue = list(targets)
    random.Random(0).shuffle(queue)
    host_slots = {stream.host: BoundedSemaphore(4) for stream in targets.values()}

    def run(target):
        stream = targets[target]
        with host_slots[stream.host]:
            return probe(stream, timeout)

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, (target, latency) in enumerate(zip(queue, executor.map(run, queue)), 1):
            results[target] = latency
            if index % 500 == 0 or index == len(queue):
                print(f"Checked {index}/{len(queue)} streams", flush=True)

    winners = []
    for streams in groups.values():
        working = [
            stream for stream in streams
            if results[stream.url, stream.options] is not None
        ]
        if working:
            winners.append(min(working, key=lambda stream: results[stream.url, stream.options]))
    return winners, len(targets)


def read_previous(url):
    if not url:
        return 0
    try:
        with urlopen(url, timeout=15) as response:
            return max(0, int(json.loads(response.read(65536))["published_channels"]))
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Use a local M3U for a test")
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--remove-first", type=int, default=5)
    parser.add_argument("--previous-status")
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.remove_first < 0:
        parser.error("Use a positive worker count and timeout, and a nonnegative removal count")
    if not shutil.which("ffprobe"):
        raise SystemExit("ffprobe is required")
    if args.input:
        raw = args.input.read_text(encoding="utf-8-sig")
    else:
        with urlopen(SOURCE, timeout=45) as response:
            raw = response.read(15_000_001)
        if len(raw) > 15_000_000:
            raise SystemExit("Source M3U exceeds size limit")
        raw = raw.decode("utf-8-sig")

    header, groups, counts = parse_playlist(raw)
    if not args.input and counts["source_entries"] < 5_000:
        raise SystemExit("Source is unexpectedly small. Keeping the previous playlist.")
    winners, tested = check(groups, args.workers, args.timeout)
    chosen = winners[args.remove_first:]
    previous = read_previous(args.previous_status)
    minimum = max(1 if args.input else 100, (previous + 1) // 2)
    if len(chosen) < minimum:
        raise SystemExit(f"Only {len(chosen)} channels passed the publication threshold of {minimum}.")

    status = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE,
        "source_entries": counts["source_entries"],
        "skipped_urls": counts["skipped_urls"],
        "unique_channels": len(groups),
        "streams_checked": tested,
        "channels_with_media": len(winners),
        "removed_leading_channels": min(args.remove_first, len(winners)),
        "published_channels": len(chosen),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    lines = [header]
    for stream in chosen:
        lines.extend((*stream.lines, stream.url))
    (args.output / "playlist.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.output / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()

"""Extract the generation units needed for zone shares from the MaStR full export.

The Marktstammdatenregister (MaStR) publishes its register as one ZIP of XML
files of about 3 GB ("Gesamtdatenexport", https://www.marktstammdatenregister.de/MaStR/Datendownload).
This script reads only the unit files it needs, straight from the URL with HTTP
range requests or from a downloaded ZIP, and writes one small CSV per file.

    python scripts/extract_mastr.py --zip <url or path of the ZIP> --out <dir>

Then run scripts/build_zone_shares.py on the output directory.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import requests

FIELDS = [
    "EinheitMastrNummer",
    "Land",
    "Bundesland",
    "Landkreis",
    "Gemeindeschluessel",
    "Laengengrad",
    "Breitengrad",
    "Inbetriebnahmedatum",
    "DatumEndgueltigeStilllegung",
    "EinheitBetriebsstatus",
    "Energietraeger",
    "Bruttoleistung",
    "Nettonennleistung",
    "WindAnLandOderAufSee",
    "Seelage",
    "NameWindpark",
    "GebietNachDemFlaechenentwicklungsplanNordsee",
    "GebietNachDemFlaechenentwicklungsplanOstsee",
    "Hauptbrennstoff",
    "Notstromaggregat",
    "Einspeisungsart",
    "Technologie",
]
STORAGE = "EinheitenStromSpeicher"
PUMPED_STORAGE = "1537"  # catalogue id of "Pumpspeicher"; the other storage units are batteries
MEMBERS = re.compile(
    r"^(EinheitenWind|EinheitenSolar_\d+|EinheitenVerbrennung|EinheitenBiomasse|EinheitenWasser"
    r"|EinheitenKernkraft|EinheitenGeothermieGrubengasDruckentspannung"
    r"|EinheitenStromSpeicher_\d+|Katalogwerte)\.xml$"
)


class RemoteFile(io.RawIOBase):
    """A seekable read-only file over HTTP range requests."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.position = 0
        response = self._get(0, 0)
        self.size = int(response.headers["Content-Range"].split("/")[1])

    def _get(self, first: int, last: int) -> requests.Response:
        """The bytes first..last; the server drops connections now and then, so retry."""
        for attempt in range(8):
            try:
                response = requests.get(
                    self.url, headers={"Range": f"bytes={first}-{last}"}, timeout=300
                )
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == 7:
                    raise
                time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        start = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = start + offset
        return self.position

    def readinto(self, buffer) -> int:
        length = min(len(buffer), self.size - self.position)
        if length <= 0:
            return 0
        last = self.position + length - 1
        response = self._get(self.position, last)
        data = response.content
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)


def open_export(source: str) -> zipfile.ZipFile:
    if source.startswith(("http://", "https://")):
        return zipfile.ZipFile(io.BufferedReader(RemoteFile(source), buffer_size=4 << 20))
    return zipfile.ZipFile(source)


def extract_member(source: str, name: str, out_dir: Path) -> None:
    target = out_dir / f"{name[:-4]}.csv"
    if target.exists():
        return
    partial = target.with_suffix(".tmp")
    with (
        open_export(source) as export,
        export.open(name) as member,
        partial.open("w", newline="", encoding="utf-8") as out,
    ):
        writer = None
        depth = 0
        for event, element in ElementTree.iterparse(member, events=("start", "end")):
            if event == "start":
                depth += 1
                continue
            depth -= 1
            if depth != 1:
                continue
            row = {child.tag: child.text for child in element}
            if writer is None:
                columns = list(row) if name.startswith("Katalog") else FIELDS
                writer = csv.DictWriter(out, columns, extrasaction="ignore")
                writer.writeheader()
            if not name.startswith(STORAGE) or row.get("Technologie") == PUMPED_STORAGE:
                writer.writerow(row)
            element.clear()
    partial.rename(target)
    print(f"wrote {target}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", required=True, dest="source", help="URL or path of the export.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    # Finished files are kept so that a broken-off run can go on, but only for one export.
    marker = args.out / "SOURCE.txt"
    earlier = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    if earlier != args.source and (earlier is not None or any(args.out.glob("*.csv"))):
        raise SystemExit(
            f"{args.out} holds files of another export ({earlier or 'unknown'}); "
            "use an empty directory"
        )
    marker.write_text(f"{args.source}\n", encoding="utf-8")
    done = args.out / "COMPLETE.txt"
    done.unlink(missing_ok=True)
    with open_export(args.source) as export:
        names = sorted(
            (name for name in export.namelist() if MEMBERS.match(name)),
            key=lambda name: name.startswith(("EinheitenSolar", STORAGE)),
        )
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(lambda name: extract_member(args.source, name, args.out), names))
    # Only written once every file is there; build_zone_shares.py asks for it.
    done.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")


if __name__ == "__main__":
    main()

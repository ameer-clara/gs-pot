from threading import Lock

from .models import Property, ScanInfo, ScanStatus


class ScanStore:
    """In-memory scan registry. File-backed persistence is a v2 concern."""

    def __init__(self) -> None:
        self._scans: dict[str, ScanInfo] = {}
        self._lock = Lock()

    def put(self, scan: ScanInfo) -> None:
        with self._lock:
            self._scans[scan.scan_id] = scan

    def get(self, scan_id: str) -> ScanInfo | None:
        with self._lock:
            return self._scans.get(scan_id)

    def list_all(self) -> list[ScanInfo]:
        with self._lock:
            return list(self._scans.values())

    def list_ready(self) -> list[ScanInfo]:
        with self._lock:
            return [s for s in self._scans.values() if s.status == ScanStatus.READY]

    def list_for_property(self, property_id: str) -> list[ScanInfo]:
        with self._lock:
            return [s for s in self._scans.values() if s.property_id == property_id]

    def clear(self) -> None:
        with self._lock:
            self._scans.clear()


class PropertyStore:
    """In-memory property (apartment/listing) registry."""

    def __init__(self) -> None:
        self._properties: dict[str, Property] = {}
        self._lock = Lock()

    def put(self, prop: Property) -> None:
        with self._lock:
            self._properties[prop.property_id] = prop

    def get(self, property_id: str) -> Property | None:
        with self._lock:
            return self._properties.get(property_id)

    def get_by_name(self, name: str) -> Property | None:
        with self._lock:
            for p in self._properties.values():
                if p.name == name:
                    return p
            return None

    def list_all(self) -> list[Property]:
        with self._lock:
            return list(self._properties.values())

    def clear(self) -> None:
        with self._lock:
            self._properties.clear()


_scan_store = ScanStore()
_property_store = PropertyStore()


def get_store() -> ScanStore:
    return _scan_store


def get_property_store() -> PropertyStore:
    return _property_store

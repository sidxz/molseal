"""molseal: encrypted structure rows in any SQL database, chemistry search in-process.

The names in `__all__` are the public surface. Everything else under `molseal.*` is reachable but may
change between releases. Key-service providers live in `molseal.providers`."""

from . import metrics
from .crypto import SealError
from .engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL, FingerprintSpec, StructureError, canonicalize
from .errors import MolsealError
from .keys import (FileKeyProvider, KeyFileError, KeyProvider, KeyProviderError, RawKeyProvider,
                   generate_key_file, provider_from_spec)
from .payload import Payload, PayloadError
from .snapshot import SnapshotError, SnapshotFormatError
from .store import SqlStore, StructureStore, VersionConflict
from .vault import NotFound, Vault, VaultError, rewrap_tenant

__version__ = "0.1.0"

__all__ = [
    "Vault", "SqlStore", "StructureStore", "rewrap_tenant",
    "KeyProvider", "FileKeyProvider", "RawKeyProvider", "provider_from_spec", "generate_key_file",
    "FingerprintSpec", "MORGAN2", "MORGAN2_CHIRAL", "FEATMORGAN2", "canonicalize", "metrics", "Payload",
    "MolsealError", "StructureError", "KeyProviderError", "KeyFileError", "VaultError", "NotFound",
    "VersionConflict", "SealError", "PayloadError", "SnapshotError", "SnapshotFormatError",
    "__version__",
]

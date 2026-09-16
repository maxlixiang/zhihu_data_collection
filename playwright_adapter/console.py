import builtins
import sys


def safe_print(*args, **kwargs):
    """Print immediately while tolerating legacy Windows console encodings."""
    kwargs["flush"] = True
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_args = [
            str(arg).encode(encoding, errors="replace").decode(encoding)
            for arg in args
        ]
        builtins.print(*safe_args, **kwargs)

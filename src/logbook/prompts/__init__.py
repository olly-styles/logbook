from importlib.resources import files


def load(name: str) -> str:
    return files(__name__).joinpath(name).read_text(encoding="utf-8").rstrip("\n")

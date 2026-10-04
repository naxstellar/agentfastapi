import uuid

import pytest

from app.services.storage import artifact_key, dataset_key, pending_dataset_key, safe_filename

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
OBJ = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sales Q1.xlsx", "Sales_Q1.xlsx"),
        ("../../etc/passwd.csv", "passwd.csv"),
        ("C:\\Users\\me\\data.CSV", "data.csv"),
        ("résumé (final).xlsx", "resume_final.xlsx"),
        ("....csv", "file.csv"),
        ("数据.csv", "file.csv"),
        ("noext", "noext"),
        ("x" * 300 + ".xlsx", "x" * 95 + ".xlsx"),
        (".bashrc", "file.bashrc"),
        ("a.tar.gz", "a.tar.gz"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_key_builders_never_use_raw_filenames():
    assert dataset_key(USER, OBJ, "../a b.csv") == f"users/{USER}/datasets/{OBJ}/a_b.csv"
    assert pending_dataset_key(USER, OBJ, "a.csv") == f"pending/users/{USER}/datasets/{OBJ}/a.csv"
    assert artifact_key(USER, OBJ, OBJ, "chart 1.png") == f"users/{USER}/artifacts/{OBJ}/{OBJ}/chart_1.png"

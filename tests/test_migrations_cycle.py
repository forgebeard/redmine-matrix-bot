import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") or "").startswith("postgresql://"),
    reason="Требуется Postgres DATABASE_URL",
)
def test_alembic_upgrade_downgrade_upgrade_cycle():
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "-1"], check=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)

from sqlalchemy import create_engine, inspect, text
from app.models import Base


def test_v06_adds_verified_tables_without_deleting_legacy_table(tmp_path):
    db=tmp_path/'migration.db'
    engine=create_engine(f'sqlite:///{db}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT)'))
        conn.execute(text("INSERT INTO legacy_marker(value) VALUES ('keep-me')"))
    Base.metadata.create_all(engine)
    names=set(inspect(engine).get_table_names())
    required={
        'system_state','token_pair_state_v06','pair_latest_price_v06','wallet_swaps_v06',
        'wallet_swap_measurements_v06','wallet_copyability_v06_verified',
        'paper_copy_trades_v06_verified','signal_measurements_v06_verified','signal_paper_trades_v06_verified'
    }
    assert required <= names
    with engine.connect() as conn:
        assert conn.execute(text('SELECT value FROM legacy_marker')).scalar_one() == 'keep-me'

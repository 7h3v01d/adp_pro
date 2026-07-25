import os

from adp.api.auth import ApiKeyStore


def test_creates_state_dir_if_missing(tmp_path):
    """Regression test: ApiKeyStore used to assume its state_dir already
    existed, so the very first run in a fresh state directory (the normal
    case!) silently failed to persist the key -- a new random key would be
    generated on every single run, and any AI tool/script configured with
    a previous key would suddenly stop authenticating."""
    nested_dir = str(tmp_path / "does" / "not" / "exist" / "yet")
    store = ApiKeyStore(nested_dir)
    key = store.key
    assert os.path.exists(store.key_file)
    with open(store.key_file) as f:
        assert f.read().strip() == key


def test_key_persists_across_instances(tmp_path):
    store1 = ApiKeyStore(str(tmp_path))
    key1 = store1.key

    store2 = ApiKeyStore(str(tmp_path))
    assert store2.key == key1


def test_regenerate_produces_a_new_key(tmp_path):
    store = ApiKeyStore(str(tmp_path))
    original = store.key
    new_key = store.regenerate()
    assert new_key != original
    assert store.key == new_key

    # And it should be the key a fresh instance loads too.
    store2 = ApiKeyStore(str(tmp_path))
    assert store2.key == new_key


def test_verify_accepts_correct_key_and_rejects_others(tmp_path):
    store = ApiKeyStore(str(tmp_path))
    key = store.key
    assert store.verify(key) is True
    assert store.verify("wrong") is False
    assert store.verify("") is False
    assert store.verify(None) is False


def test_key_is_reasonably_random_and_long(tmp_path):
    store = ApiKeyStore(str(tmp_path))
    key = store.key
    assert len(key) >= 32
    store2 = ApiKeyStore(str(tmp_path / "other"))
    assert key != store2.key

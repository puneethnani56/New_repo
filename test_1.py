from note import list_sum  # replace `your_module` with the actual file name (without `.py`)

def test_custom_sum():
    a = list(range(1, 100, 2))  # [1, 3, 5, ..., 99]
    expected = list_sum(a)  # built-in sum for validation
    return expected
assert test_custom_sum() == sum(list_sum())

from note import list_sum  # replace `your_module` with the actual file name (without `.py`)

a = list(range(1, 100, 2))  # [1, 3, 5, ..., 99]
expected = list_sum(a)  # built-in sum for validation
def test_custom_sum(l):
    expected = sum(l)
    return expected
assert test_custom_sum(a) == sum(list_sum(a))

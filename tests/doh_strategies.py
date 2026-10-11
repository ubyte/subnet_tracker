from hypothesis import strategies as st

from subnet_tracker.types import JsonValue

_keys = st.sampled_from(["Status", "Question", "Answer", "name", "type", "TTL", "data", "resolver_id", "http_status", "body", "error"]) | st.text(max_size=5)
_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.sampled_from(["A", "AAAA", "example.com", "example.com.", "1.2.3.4", "::1"]) | st.text(max_size=10)

json_values: st.SearchStrategy[JsonValue] = st.recursive(_scalars, lambda children: st.lists(children, max_size=4) | st.dictionaries(_keys, children, max_size=6), max_leaves=30)

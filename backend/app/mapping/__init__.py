"""
mapping — Object & Column Mapping engine (Phase 4 of the data-integration
build).

datatype_mapper.py  — source/target type-family compatibility scoring
auto_mapper.py       — real algorithmic column matching (exact, case-
                        insensitive, normalized, alias-dictionary,
                        similarity + type compatibility). AI-assisted
                        matching from the spec is explicitly NOT
                        implemented here — see that module's docstring —
                        rather than faked with a canned response.
mapper.py             — validation (conflict/duplicate-target detection,
                        unmapped columns) and the SQL builder that turns a
                        mapping definition into a real SELECT for preview.
"""

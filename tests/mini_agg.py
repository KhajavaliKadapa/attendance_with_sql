"""A MODEL of the small part of MongoDB's aggregation language that app/main.py uses.

It is NOT MongoDB. It is my reading of the MongoDB documentation, written as code, so the real
pipeline builders can be executed without a server. What it can catch: typos in field or variable
names, wrong $let nesting, wrong array shapes, wrong stage order. What it cannot catch: a place
where real MongoDB behaves differently from my reading of the docs. Only a run against a real
server (pytest -m integration) settles that.

Anything it does not know raises NotImplementedError instead of guessing.
"""
import copy
import functools
from datetime import datetime, timedelta, timezone

MISSING = object()  # an absent field (different from null)


# ------------------------------------------------------------------ helpers
def truthy(v):
    return v is not MISSING and v is not None and v is not False and v != 0


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def type_rank(v):  # MongoDB's cross-type order, reduced to the types used here
    if v is MISSING or v is None:
        return 0
    if is_num(v):
        return 1
    if isinstance(v, str):
        return 2
    if isinstance(v, bool):
        return 3
    if isinstance(v, datetime):
        return 4
    raise NotImplementedError(f"compare {type(v)}")


def compare(a, b):
    ra, rb = type_rank(a), type_rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 0:
        return 0
    return (a > b) - (a < b)


def get_path(v, parts):
    """'a.b' on a dict; through an array it collects the values (as MongoDB does)."""
    if not parts:
        return v
    if isinstance(v, dict):
        return get_path(v[parts[0]], parts[1:]) if parts[0] in v else MISSING
    if isinstance(v, list):
        out = [get_path(x, parts) for x in v]
        return [o for o in out if o is not MISSING]
    return MISSING


# ------------------------------------------------------------------ the model
class Model:
    def __init__(self, collections: dict):
        self.collections = collections  # name -> list of documents

    # ---------------------------------------------------------- expressions
    def ev(self, e, doc, scope):
        if isinstance(e, str):
            if e.startswith("$$"):
                name, *rest = e[2:].split(".")
                if name == "ROOT":
                    base = doc
                elif name in scope:
                    base = scope[name]
                else:
                    raise KeyError(f"undefined variable ${name} in {e!r}")
                return get_path(base, rest) if rest else base
            if e.startswith("$"):
                return get_path(doc, e[1:].split("."))
            return e
        if isinstance(e, list):
            return [self.ev(x, doc, scope) for x in e]
        if isinstance(e, dict):
            if len(e) == 1 and next(iter(e)).startswith("$"):
                name, arg = next(iter(e.items()))
                return self.op(name, arg, doc, scope)
            out = {}
            for k, v in e.items():
                val = self.ev(v, doc, scope)
                if val is not MISSING:
                    out[k] = val
            return out
        return e

    def op(self, name, arg, doc, scope):  # noqa: C901 - one flat dispatch is the clearest here
        ev = lambda x: self.ev(x, doc, scope)  # noqa: E731
        if name == "$cond":
            c, t, f = (arg["if"], arg["then"], arg["else"]) if isinstance(arg, dict) else arg
            return ev(t) if truthy(ev(c)) else ev(f)
        if name == "$and":
            return all(truthy(ev(x)) for x in arg)
        if name in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte"):
            a, b = ev(arg[0]), ev(arg[1])
            if name in ("$eq", "$ne"):
                same = (a is MISSING and b is MISSING) or (a is not MISSING and b is not MISSING and a == b)
                return same if name == "$eq" else not same
            c = compare(a, b)
            return {"$gt": c > 0, "$gte": c >= 0, "$lt": c < 0, "$lte": c <= 0}[name]
        if name == "$in":
            x, arr = ev(arg[0]), ev(arg[1])
            if not isinstance(arr, list):
                raise TypeError("$in needs an array")
            return x in arr
        if name == "$ifNull":
            a = ev(arg[0])
            return ev(arg[1]) if a is None or a is MISSING else a
        if name == "$isNumber":
            return is_num(ev(arg))
        if name == "$dateFromString":
            if arg["format"] != "%Y-%m-%d":
                raise NotImplementedError(arg["format"])
            return datetime.strptime(ev(arg["dateString"]), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if name == "$isoDayOfWeek":
            return ev(arg).isoweekday()
        if name == "$dateAdd":
            if arg["unit"] != "day":
                raise NotImplementedError(arg["unit"])
            return ev(arg["startDate"]) + timedelta(days=ev(arg["amount"]))
        if name == "$dateToString":
            if arg["format"] != "%Y-%m-%d":
                raise NotImplementedError(arg["format"])
            return ev(arg["date"]).strftime("%Y-%m-%d")
        if name == "$range":
            lo, hi = ev(arg[0]), ev(arg[1])
            return list(range(lo, hi))
        if name == "$map":
            return [self.ev(arg["in"], doc, {**scope, arg["as"]: x}) for x in ev(arg["input"])]
        if name == "$filter":
            return [x for x in ev(arg["input"]) if truthy(self.ev(arg["cond"], doc, {**scope, arg["as"]: x}))]
        if name == "$arrayElemAt":
            arr, i = ev(arg[0]), ev(arg[1])
            return arr[i] if 0 <= i < len(arr) else MISSING
        if name == "$reduce":
            acc = ev(arg["initialValue"])
            for x in ev(arg["input"]):
                acc = self.ev(arg["in"], doc, {**scope, "this": x, "value": acc})
            return acc
        if name == "$add":
            return sum(ev(x) for x in arg)
        if name == "$divide":
            return ev(arg[0]) / ev(arg[1])
        if name == "$let":
            inner = dict(scope)
            for k, v in arg["vars"].items():
                inner[k] = self.ev(v, doc, scope)  # vars see the OUTER scope only, as in MongoDB
            return self.ev(arg["in"], doc, inner)
        if name == "$sum":  # expression form: sums an array, or a list of operands
            vals = [ev(x) for x in arg] if isinstance(arg, list) else ev(arg)
            vals = vals if isinstance(vals, list) else [vals]
            return sum(v for v in vals if is_num(v))
        raise NotImplementedError(f"expression operator {name}")

    # ---------------------------------------------------------- query matching ($match)
    def matches(self, doc, query, scope):
        for key, cond in query.items():
            if key == "$expr":
                if not truthy(self.ev(cond, doc, scope)):
                    return False
                continue
            val = get_path(doc, key.split("."))
            if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
                for op, rhs in cond.items():
                    if val is MISSING or val is None or type_rank(val) != type_rank(rhs):
                        return False  # query operators are type-bracketed: null never matches $gt 0 etc.
                    c = compare(val, rhs)
                    if not {"$gt": c > 0, "$gte": c >= 0, "$lt": c < 0, "$lte": c <= 0}[op]:
                        return False
            elif cond is None:
                if not (val is None or val is MISSING):
                    return False
            elif val is MISSING or val != cond:
                return False
        return True

    # ---------------------------------------------------------- stages
    def run(self, docs, pipeline, scope=None):
        scope = scope or {}
        docs = [copy.deepcopy(d) for d in docs]
        for stage in pipeline:
            (name, spec), = stage.items()
            handler = getattr(self, "stage_" + name[1:], None)
            if handler is None:
                raise NotImplementedError(f"stage {name}")
            docs = handler(docs, spec, scope)
        return docs

    def stage_match(self, docs, spec, scope):
        return [d for d in docs if self.matches(d, spec, scope)]

    def stage_group(self, docs, spec, scope):
        groups = {}
        for d in docs:
            key = self.ev(spec["_id"], d, scope)
            key = None if key is MISSING else key
            g = groups.setdefault(key, {"_id": key})
            for field, acc in spec.items():
                if field == "_id":
                    continue
                (op, expr), = acc.items()
                v = self.ev(expr, d, scope)
                if op == "$sum":
                    g[field] = g.get(field, 0) + (v if is_num(v) else 0)
                elif op == "$push":
                    g.setdefault(field, [])
                    if v is not MISSING:
                        g[field].append(v)
                else:
                    raise NotImplementedError(f"accumulator {op}")
        return list(groups.values())

    def stage_sort(self, docs, spec, scope):
        def cmp(a, b):
            for field, direction in spec.items():
                c = compare(get_path(a, field.split(".")), get_path(b, field.split(".")))
                if c:
                    return c * direction
            return 0
        return sorted(docs, key=functools.cmp_to_key(cmp))

    def stage_lookup(self, docs, spec, scope):
        unknown = set(spec) - {"from", "as", "localField", "foreignField", "let", "pipeline"}
        if unknown:
            raise NotImplementedError(f"$lookup keys {unknown}")
        if ("localField" in spec) != ("foreignField" in spec):
            raise ValueError("$lookup needs localField and foreignField together")
        foreign = self.collections[spec["from"]]
        out = []
        for d in docs:
            candidates = foreign
            if "localField" in spec:  # equality join first ...
                lv = get_path(d, spec["localField"].split("."))
                candidates = [f for f in foreign
                              if lv is not MISSING and get_path(f, spec["foreignField"].split(".")) == lv]
            if "pipeline" in spec:    # ... then the sub-pipeline over the matches (MongoDB 5.0+ concise form)
                inner = dict(scope)
                for k, v in spec.get("let", {}).items():
                    inner[k] = self.ev(v, d, scope)
                found = self.run(candidates, spec["pipeline"], inner)
            elif "localField" in spec:
                found = [copy.deepcopy(f) for f in candidates]
            else:
                raise ValueError("$lookup needs localField/foreignField or a pipeline")
            out.append({**d, spec["as"]: found})
        return out

    def stage_unwind(self, docs, spec, scope):
        path, keep = (spec, False) if isinstance(spec, str) else (spec["path"], spec.get("preserveNullAndEmptyArrays", False))
        field = path[1:]
        out = []
        for d in docs:
            arr = d.get(field, MISSING)
            if isinstance(arr, list) and arr:
                out += [{**d, field: x} for x in arr]
            elif arr is MISSING or arr is None or arr == []:
                if keep:
                    rest = {k: v for k, v in d.items() if k != field}
                    out.append(rest)
            else:
                out.append(d)
        return out

    def stage_project(self, docs, spec, scope):
        out = []
        for d in docs:
            r = {}
            if spec.get("_id", 1) not in (0, False) and "_id" in d:
                r["_id"] = d["_id"]
            for k, v in spec.items():
                if k == "_id" and v in (0, 1, True, False):
                    continue
                if v in (1, True) and not isinstance(v, (dict, str)):
                    if k in d:
                        r[k] = d[k]
                elif v in (0, False) and not isinstance(v, (dict, str)):
                    raise NotImplementedError("exclusion")
                else:
                    val = self.ev(v, d, scope)
                    if val is not MISSING:
                        r[k] = val
            out.append(r)
        return out

    def stage_replaceRoot(self, docs, spec, scope):
        return [self.ev(spec["newRoot"], d, scope) for d in docs]

    def stage_setWindowFields(self, docs, spec, scope):
        if "partitionBy" in spec:
            raise NotImplementedError("partitionBy")
        (sort_field, direction), = spec["sortBy"].items()
        ordered = self.stage_sort(docs, {sort_field: direction}, scope)
        keys = [get_path(d, sort_field.split(".")) for d in ordered]
        for i, d in enumerate(ordered):
            for out_name, definition in spec["output"].items():
                if "$rank" in definition:
                    d[out_name] = 1 + next(j for j, k in enumerate(keys) if compare(k, keys[i]) == 0)  # ties share a rank
                elif "$avg" in definition:
                    lo, hi = definition["window"]["documents"]
                    window = ordered[max(0, i + lo): i + hi + 1]
                    vals = [v for v in (self.ev(definition["$avg"], w, scope) for w in window) if is_num(v)]
                    d[out_name] = sum(vals) / len(vals) if vals else None
                else:
                    raise NotImplementedError(definition)
        return ordered


# ------------------------------------------------------------------ a stand-in for `db`
class ModelCollection:
    def __init__(self, model, name):
        self.model, self.name = model, name

    def aggregate(self, pipeline):
        return iter(self.model.run(self.model.collections[self.name], pipeline))

    def find_one(self, flt, projection=None, **_):
        for d in self.model.collections[self.name]:
            if self.model.matches(d, flt, {}):
                return copy.deepcopy(d)
        return None


class ModelDB:
    """Quacks like the two collections the analytics endpoints touch."""

    def __init__(self, employees, logs):
        model = Model({"employees": employees, "attendance_logs": logs})
        self.employees = ModelCollection(model, "employees")
        self.attendance_logs = ModelCollection(model, "attendance_logs")

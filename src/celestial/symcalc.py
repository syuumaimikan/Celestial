"""数式・方程式・微積分・統計・単位変換の記号計算エンジン(SymPy 使用・任意コード実行は不可)。"""

from __future__ import annotations

import math
import re

_FORBIDDEN = re.compile(r"__|import|eval|exec|open|lambda|getattr|setattr|delattr|globals|locals|\b(?:sys|os|subprocess)\b", re.I)

_UNIT_FACTORS: dict[str, tuple[str, float]] = {
    # 長さ (基準: m)
    "mm": ("length", 1e-3), "cm": ("length", 1e-2), "m": ("length", 1.0), "km": ("length", 1e3),
    "inch": ("length", 0.0254), "in": ("length", 0.0254), "ft": ("length", 0.3048),
    "yd": ("length", 0.9144), "mile": ("length", 1609.344), "miles": ("length", 1609.344),
    # 質量 (基準: kg)
    "mg": ("mass", 1e-6), "g": ("mass", 1e-3), "kg": ("mass", 1.0), "t": ("mass", 1e3), "ton": ("mass", 1e3),
    "oz": ("mass", 0.028349523125), "lb": ("mass", 0.45359237), "lbs": ("mass", 0.45359237),
    # 面積 (基準: m2)
    "m2": ("area", 1.0), "km2": ("area", 1e6), "ha": ("area", 1e4), "a": ("area", 100.0), "tsubo": ("area", 400 / 121),
    # 体積 (基準: l)
    "ml": ("volume", 1e-3), "l": ("volume", 1.0), "liter": ("volume", 1.0), "m3": ("volume", 1e3), "gal": ("volume", 3.785411784),
    # 時間 (基準: s)
    "ms": ("time", 1e-3), "s": ("time", 1.0), "sec": ("time", 1.0), "min": ("time", 60.0),
    "h": ("time", 3600.0), "hr": ("time", 3600.0), "day": ("time", 86400.0), "week": ("time", 604800.0),
    # 速度 (基準: m/s)
    "m/s": ("speed", 1.0), "km/h": ("speed", 1 / 3.6), "kph": ("speed", 1 / 3.6), "mph": ("speed", 0.44704), "knot": ("speed", 1852 / 3600),
    # データ量 (基準: B)
    "b": ("data", 1.0), "kb": ("data", 1e3), "mb": ("data", 1e6), "gb": ("data", 1e9), "tb": ("data", 1e12),
    "kib": ("data", float(1 << 10)), "mib": ("data", float(1 << 20)), "gib": ("data", float(1 << 30)), "tib": ("data", float(1 << 40)),
    # 圧力 (基準: Pa)
    "pa": ("pressure", 1.0), "hpa": ("pressure", 100.0), "kpa": ("pressure", 1e3), "bar": ("pressure", 1e5), "atm": ("pressure", 101325.0),
    # エネルギー (基準: J)
    "j": ("energy", 1.0), "kj": ("energy", 1e3), "cal": ("energy", 4.184), "kcal": ("energy", 4184.0), "wh": ("energy", 3600.0), "kwh": ("energy", 3.6e6),
}
_UNIT_ALIAS = {
    "メートル": "m", "キロメートル": "km", "センチ": "cm", "センチメートル": "cm", "ミリ": "mm", "ミリメートル": "mm",
    "マイル": "mile", "フィート": "ft", "インチ": "inch", "グラム": "g", "キログラム": "kg", "トン": "ton", "ポンド": "lb",
    "リットル": "l", "ミリリットル": "ml", "坪": "tsubo", "ヘクタール": "ha", "秒": "s", "分": "min", "時間": "h", "日": "day",
    "時速": "km/h", "km/h": "km/h", "m/s": "m/s", "bytes": "b", "byte": "b", "摂氏": "c", "華氏": "f", "ケルビン": "k",
    "°c": "c", "℃": "c", "°f": "f", "℉": "f",
}
_UNIT_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([^\s=]+)\s*(?:to|in|->|を|から)\s*([^\s=]+)\s*$", re.I)


def _norm_unit(u: str) -> str:
    low = u.strip().lower()
    return _UNIT_ALIAS.get(low, _UNIT_ALIAS.get(u.strip(), low))


def _format_num(val: float) -> str:
    if abs(val - round(val)) < 1e-12 and abs(val) < 1e15:
        return str(int(round(val)))
    return f"{val:.8g}".rstrip("0").rstrip(".") if "." in f"{val:.8g}" else f"{val:.8g}"


def _convert_units(expr: str) -> str | None:
    m = _UNIT_RE.match(expr.replace(",", ""))
    if not m:
        return None
    val = float(m.group(1))
    src = _norm_unit(m.group(2))
    dst = _norm_unit(m.group(3))
    temps = {"c", "f", "k"}
    if src in temps and dst in temps:
        c = val if src == "c" else (val - 32.0) * 5.0 / 9.0 if src == "f" else val - 273.15
        out = c if dst == "c" else c * 9.0 / 5.0 + 32.0 if dst == "f" else c + 273.15
        return f"{_format_num(out)} {m.group(3)}"
    s_info = _UNIT_FACTORS.get(src)
    d_info = _UNIT_FACTORS.get(dst)
    if s_info and d_info and s_info[0] == d_info[0]:
        out = val * s_info[1] / d_info[1]
        return f"{_format_num(out)} {m.group(3)}"
    return None


def _clean_math(text: str) -> str:
    text = text.strip().replace("×", "*").replace("÷", "/").replace("−", "-").replace("π", "pi")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = re.sub(r"(\d+)\s*%", r"(\1/100)", text)
    return text.replace("^", "**")


def _parse(expr: str, local_dict: dict):
    from tokenize import TokenError
    from sympy.parsing.sympy_parser import convert_xor, implicit_multiplication_application, parse_expr, standard_transformations

    trans = standard_transformations + (implicit_multiplication_application, convert_xor)
    try:
        return parse_expr(expr, local_dict=local_dict, transformations=trans, evaluate=True)
    except TokenError as e:
        raise SyntaxError(str(e)) from e


def _format_sym(val) -> str:
    import sympy as sp

    if isinstance(val, dict):
        items = [f"{k} = {_format_sym(v)}" for k, v in val.items()]
        return ", ".join(items)
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(_format_sym(v) for v in val) + "]"
    s = str(val)
    try:
        if getattr(val, "is_number", False) and val.is_real and not val.is_Integer:
            f = float(val.evalf())
            if math.isfinite(f):
                approx = _format_num(f)
                if s != approx and len(s) <= 40:
                    return f"{s} (≈ {approx})"
                return approx
    except (TypeError, ValueError):
        pass
    return s


def sym_evaluate(expression: str) -> str | None:
    """方程式・微分・積分・因数分解・素因数分解・統計・単位変換を SymPy で解く。非対応/純算術なら None。"""
    expr = expression.strip()
    if not expr or len(expr) > 400 or _FORBIDDEN.search(expr):
        return None
    unit_res = _convert_units(expr)
    if unit_res is not None:
        return unit_res

    clean = _clean_math(expr)
    has_command = bool(re.match(r"^(?:solve|simplify|factor|expand|diff|integrate|limit|factorint|isprime|mean|median|stdev)\s*\(", clean))
    is_solve = clean.startswith("solve(") and clean.endswith(")")
    has_equation = "=" in clean and not is_solve and not has_command and not re.search(r"[<>!]=|==", clean)
    if not has_command and not has_equation:
        return None

    import sympy as sp

    sym_names = {ch: sp.Symbol(ch) for ch in "xyzabcnmkit"}
    env = {
        **sym_names,
        "pi": sp.pi, "E": sp.E, "oo": sp.oo, "inf": sp.oo,
        "sqrt": sp.sqrt, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
        "asin": sp.asin, "acos": sp.acos, "atan": sp.atan,
        "log": sp.log, "ln": sp.log, "exp": sp.exp, "abs": sp.Abs,
        "factorial": sp.factorial, "binomial": sp.binomial, "comb": sp.binomial,
        "perm": lambda n, k=None: sp.factorial(n) if k is None else sp.factorial(n) / sp.factorial(n - k),
        "gcd": sp.gcd, "lcm": sp.lcm,
        "mean": lambda *xs: sp.Rational(sum(xs), len(xs)),
        "median": lambda *xs: sp.nsimplify(sorted(xs)[len(xs) // 2]) if len(xs) % 2 else (sorted(xs)[len(xs) // 2 - 1] + sorted(xs)[len(xs) // 2]) / 2,
        "stdev": lambda *xs: sp.sqrt(sum((v - sum(xs) / len(xs)) ** 2 for v in xs) / max(1, len(xs) - 1)),
        "factorint": sp.factorint, "isprime": sp.isprime,
        "simplify": sp.simplify, "factor": sp.factor, "expand": sp.expand,
        "diff": sp.diff, "integrate": sp.integrate, "limit": sp.limit,
    }

    if has_equation:
        parts = [p.strip() for p in re.split(r"[,;、]", clean) if p.strip()]
        eqs = []
        free: set = set()
        for part in parts:
            if "=" not in part:
                raise ValueError("方程式の形式が正しくありません")
            lhs, rhs = part.split("=", 1)
            le = _parse(lhs, env)
            re_ = _parse(rhs, env)
            eq = sp.Eq(le, re_)
            eqs.append(eq)
            free |= eq.free_symbols
        if not free:
            raise ValueError("解く変数がありません")
        target = sorted(free, key=lambda s: s.name)
        sol = sp.solve(eqs if len(eqs) > 1 else eqs[0], target if len(target) > 1 else target[0])
        if isinstance(sol, dict):
            return ", ".join(f"{k} = {_format_sym(v)}" for k, v in sol.items())
        if isinstance(sol, list):
            if not sol:
                return "解なし"
            if len(target) == 1:
                return f"{target[0]} = " + ", ".join(_format_sym(s) for s in sol)
            return ", ".join(_format_sym(s) for s in sol)
        return _format_sym(sol)

    # solve(...) 呼び出し
    if clean.startswith("solve(") and clean.endswith(")"):
        inner = clean[6:-1].strip()
        # 上位レベルのカンマで引数分割
        args = _split_args(inner)
        if not args:
            raise ValueError("solve の引数が空です")
        eq_txt = args[0]
        if "=" in eq_txt:
            lhs, rhs = eq_txt.split("=", 1)
            target_expr = sp.Eq(_parse(lhs, env), _parse(rhs, env))
        else:
            target_expr = _parse(eq_txt, env)
        if len(args) >= 2:
            vars_list = [_parse(v, env) for v in args[1:]]
            sol = sp.solve(target_expr, vars_list if len(vars_list) > 1 else vars_list[0])
            name = ", ".join(str(v) for v in vars_list)
        else:
            syms = sorted(target_expr.free_symbols, key=lambda s: s.name)
            sol = sp.solve(target_expr, syms if len(syms) > 1 else (syms[0] if syms else sp.Symbol("x")))
            name = str(syms[0]) if len(syms) == 1 else ", ".join(str(s) for s in syms)
        if isinstance(sol, dict):
            return ", ".join(f"{k} = {_format_sym(v)}" for k, v in sol.items())
        if isinstance(sol, list):
            return (f"{name} = " if name else "") + (", ".join(_format_sym(s) for s in sol) if sol else "解なし")
        return _format_sym(sol)

    val = _parse(clean, env)
    return _format_sym(val)


def _split_args(text: str) -> list[str]:
    out, buf, depth = [], [], 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            item = "".join(buf).strip()
            if item:
                out.append(item)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out

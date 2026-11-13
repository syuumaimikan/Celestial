"""QRコードの生成(バイトモード・誤り訂正レベル M・型番1〜40)。標準ライブラリだけで SVG を作る。

ISO/IEC 18004 の手順どおり: データ符号化 → リード・ソロモン誤り訂正 → ブロックの交互配置 → 機能パターン →
ジグザグ配置 → 8種のマスクから減点の最も少ないものを選ぶ → 形式情報・型番情報。
"""

from __future__ import annotations

# 誤り訂正レベル M の、型番ごとのブロック数と1ブロックあたりの誤り訂正語数
_ECC_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                  26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
           17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_FORMAT_BITS_M = 0  # 形式情報での M の値


def _raw_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        align = version // 7 + 2
        result -= (25 * align - 10) * align - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[version] * _BLOCKS[version]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    count = version // 7 + 2
    step = 26 if version == 32 else (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    positions = [version * 4 + 10 - i * step for i in range(count - 1)]
    return [6] + sorted(positions)


class QrCode:
    def __init__(self, data: bytes):
        for version in range(1, 41):
            cci_bits = 8 if version <= 9 else 16
            if 4 + cci_bits + len(data) * 8 <= _data_codewords(version) * 8:
                break
        else:
            raise ValueError("QRコードに入りきりません")
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self._function = [[False] * self.size for _ in range(self.size)]

        # データのビット列(モード指示子 0100 = バイト、文字数、本体、終端、詰め物)
        bits: list[int] = []

        def put(value: int, length: int) -> None:
            bits.extend((value >> i) & 1 for i in reversed(range(length)))

        put(0b0100, 4)
        put(len(data), cci_bits)
        for b in data:
            put(b, 8)
        capacity = _data_codewords(version) * 8
        put(0, min(4, capacity - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        best, best_score = 0, None
        for mask in range(8):  # 読み取りやすさの減点が最も少ないマスクを選ぶ
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            score = self._penalty()
            if best_score is None or score < best_score:
                best, best_score = mask, score
            self._apply_mask(mask)  # XOR なので2回かけると元に戻る
        self._apply_mask(best)
        self._draw_format_bits(best)

    # ---- 機能パターン ----

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._function[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):  # タイミングパターン
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # 位置検出パターン(と分離パターン)
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version)
        last = len(positions) - 1
        for i, ax in enumerate(positions):  # 位置合わせパターン
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)  # 場所の予約(あとで正しい値を書く)
        if self.version >= 7:  # 型番情報
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            value = self.version << 12 | rem
            for i in range(18):
                bit = (value >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _draw_format_bits(self, mask: int) -> None:
        data = _FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        size = self.size
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)  # 常に暗いモジュール

    # ---- データ ----

    def _add_ecc(self, data: list[int]) -> list[int]:
        version = self.version
        blocks_n, ecc_len = _BLOCKS[version], _ECC_PER_BLOCK[version]
        raw = _raw_modules(version) // 8
        short_blocks = blocks_n - raw % blocks_n
        short_len = raw // blocks_n
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(blocks_n):
            length = short_len - ecc_len + (0 if i < short_blocks else 1)
            chunk = data[k:k + length]
            k += length
            ecc = _rs_remainder(chunk, divisor)
            if i < short_blocks:
                chunk = chunk + [0]
            blocks.append(chunk + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_blocks:
                    result.append(block[i])
        return result

    def _draw_codewords(self, data: list[int]) -> None:
        size, i = self.size, 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self._function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        tests = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
                 lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
                 lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
                 lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)
        test = tests[mask]
        for y in range(self.size):
            row, fn = self.modules[y], self._function[y]
            for x in range(self.size):
                if not fn[x] and test(x, y):
                    row[x] = not row[x]

    def _penalty(self) -> int:
        size, m = self.size, self.modules
        score = 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run, prev = 0, None
            for cell in line:  # 同じ色が5つ以上続く
                if cell == prev:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    run, prev = 1, cell
            for i in range(size - 10):  # 位置検出パターンに似た並び
                window = line[i:i + 11]
                if window == finder_a or window == finder_b:
                    score += 40
        for y in range(size - 1):  # 2×2 の同色
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = size * size
        score += abs(dark * 20 - total * 10) // total * 10  # 暗いモジュールの割合が50%から外れるほど減点
        return score

    # ---- 出力 ----

    def svg(self, border: int = 4, dark: str = "#111827", light: str = "#ffffff") -> str:
        size = self.size + border * 2
        path = "".join(f"M{x + border} {y + border}h1v1h-1z" for y in range(self.size) for x in range(self.size) if self.modules[y][x])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
                f'<rect width="100%" height="100%" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')


def qr_svg(text: str, **kw) -> str:
    return QrCode(text.encode("utf-8")).svg(**kw)

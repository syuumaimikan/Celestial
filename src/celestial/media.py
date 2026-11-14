"""画像処理(リサイズ・切り抜き・回転・変換・PDF化・透かし・コラージュ)と QR/バーコード生成・読み取り。

Pillow と zxing-cpp、および内蔵の qr.py を組み合わせる。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import qr as qr_mod


def image_info(path: Path) -> str:
    from PIL import ExifTags, Image

    with Image.open(path) as img:
        info = {
            "path": path.name,
            "format": img.format,
            "mode": img.mode,
            "width": img.width,
            "height": img.height,
            "bytes": path.stat().st_size,
        }
        exif_raw = img.getexif() if hasattr(img, "getexif") else None
        if exif_raw:
            exif = {}
            for k, v in list(exif_raw.items())[:12]:
                tag = ExifTags.TAGS.get(k, str(k))
                if isinstance(v, (str, int, float)):
                    exif[str(tag)] = v
            if exif:
                info["exif"] = exif
    return json.dumps(info, ensure_ascii=False, indent=2)


def image_edit(src: Path, dest: Path, action: str, *, width: int | None = None, height: int | None = None,
               box: tuple[int, int, int, int] | None = None, angle: float = 0.0, text: str = "",
               extras: list[Path] | None = None) -> str:
    """画像を加工して dest に保存する。dest の拡張子で出力形式(png/jpg/webp/bmp/ico/pdf)を決める。"""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    dest.parent.mkdir(parents=True, exist_ok=True)
    act = action.lower().strip()
    if act == "collage":
        paths = [src, *(extras or [])]
        imgs = [ImageOps.exif_transpose(Image.open(p)).convert("RGB") for p in paths if p.is_file()]
        if not imgs:
            raise ValueError("コラージュする画像がありません")
        cols = max(1, int(round(len(imgs) ** 0.5)))
        rows = (len(imgs) + cols - 1) // cols
        cell_w = max(w for w in (width or 480, 120))
        cell_h = max(h for h in (height or 360, 90))
        canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (245, 247, 250))
        for idx, im in enumerate(imgs):
            fitted = ImageOps.contain(im, (cell_w - 8, cell_h - 8))
            r, c = divmod(idx, cols)
            x = c * cell_w + (cell_w - fitted.width) // 2
            y = r * cell_h + (cell_h - fitted.height) // 2
            canvas.paste(fitted, (x, y))
        canvas.save(dest)
        return f"コラージュを作成しました: {dest.name} ({canvas.width}x{canvas.height}, {len(imgs)}枚)"

    with Image.open(src) as opened:
        img = ImageOps.exif_transpose(opened)
        if act == "resize":
            if not width and not height:
                raise ValueError("resize には width か height が必要です")
            tw = width or max(1, round(img.width * (height / img.height)))
            th = height or max(1, round(img.height * (width / img.width)))
            img = img.resize((max(1, tw), max(1, th)), Image.Resampling.LANCZOS)
        elif act == "crop":
            if not box or len(box) != 4:
                raise ValueError("crop には left,top,right,bottom の4数値が必要です")
            left, top, right, bottom = box
            img = img.crop((max(0, left), max(0, top), min(img.width, right), min(img.height, bottom)))
        elif act == "rotate":
            img = img.rotate(-angle, expand=True)
        elif act == "flip_h":
            img = ImageOps.mirror(img)
        elif act == "flip_v":
            img = ImageOps.flip(img)
        elif act == "grayscale":
            img = ImageOps.grayscale(img)
        elif act == "blur":
            img = img.filter(ImageFilter.GaussianBlur(radius=max(1.0, angle or 2.0)))
        elif act == "sharpen":
            img = ImageEnhance.Sharpness(img).enhance(max(0.5, angle or 2.0))
        elif act == "contrast":
            img = ImageEnhance.Contrast(img).enhance(max(0.1, angle or 1.4))
        elif act == "watermark":
            if not text.strip():
                raise ValueError("watermark には text が必要です")
            base = img.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            pad = max(12, min(base.width, base.height) // 30)
            draw.rectangle((pad - 6, base.height - pad - 24, base.width - pad + 6, base.height - pad + 6), fill=(15, 23, 42, 160))
            draw.text((pad, base.height - pad - 20), text.strip()[:80], fill=(255, 255, 255, 235))
            img = Image.alpha_composite(base, overlay)
        elif act != "convert":
            raise ValueError(f"未対応の画像操作: {action} (info/resize/crop/rotate/flip_h/flip_v/grayscale/blur/sharpen/contrast/watermark/convert/collage)")

        suffix = dest.suffix.lower()
        if suffix in (".jpg", ".jpeg", ".bmp", ".pdf") and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img.save(dest)
        return f"画像を保存しました: {dest.name} ({img.width}x{img.height})"


def generate_barcode(text: str, dest: Path, *, fmt: str = "qr", scale: int = 8) -> str:
    """QRコード (.svg / .png) または各種バーコード (.png) を生成して保存する。"""
    if not text:
        raise ValueError("埋め込む内容 (text) が空です")
    dest.parent.mkdir(parents=True, exist_ok=True)
    kind = fmt.lower().strip()
    if dest.suffix.lower() == ".svg" and kind in ("qr", "qrcode", ""):
        dest.write_text(qr_mod.qr_svg(text), encoding="utf-8")
        return f"QRコード(SVG)を作成しました: {dest.name}"

    import zxingcpp
    from PIL import Image

    fmt_map = {
        "qr": zxingcpp.BarcodeFormat.QRCode,
        "qrcode": zxingcpp.BarcodeFormat.QRCode,
        "code128": zxingcpp.BarcodeFormat.Code128,
        "code39": zxingcpp.BarcodeFormat.Code39,
        "ean13": zxingcpp.BarcodeFormat.EAN13,
        "ean8": zxingcpp.BarcodeFormat.EAN8,
        "upca": zxingcpp.BarcodeFormat.UPCA,
        "datamatrix": zxingcpp.BarcodeFormat.DataMatrix,
        "aztec": zxingcpp.BarcodeFormat.Aztec,
        "pdf417": zxingcpp.BarcodeFormat.PDF417,
    }
    bf = fmt_map.get(kind)
    if bf is None:
        raise ValueError(f"未対応の形式です: {fmt} (対応: {', '.join(fmt_map)})")
    scale = max(1, min(20, int(scale)))
    bc = zxingcpp.create_barcode(text, bf)
    # 1D バーコードは縦を広げる、2D は等倍拡大
    is_1d = kind in ("code128", "code39", "ean13", "ean8", "upca")
    import numpy as np

    img_arr = np.asarray(bc.to_image(scale=scale, add_hrt=is_1d, add_quiet_zones=True))
    pil = Image.fromarray(img_arr)
    if is_1d and pil.height < 80:
        pil = pil.resize((pil.width, max(80, pil.height * scale * 6)), Image.Resampling.NEAREST)
    pil.save(dest)
    return f"バーコード/QR({kind.upper()})を作成しました: {dest.name} ({pil.width}x{pil.height})"


def scan_barcode(path: Path) -> str:
    """画像ファイルから QR コードやバーコードを読み取る。"""
    import zxingcpp
    from PIL import Image

    with Image.open(path) as img:
        results = zxingcpp.read_barcodes(img.convert("RGB"))
    if not results:
        return "QRコード・バーコードは検出されませんでした"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r.format.name}] {r.text}")
    return "\n".join(lines)

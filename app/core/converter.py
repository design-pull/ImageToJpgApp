# app/core/converter.py
# Image open / conversion utilities for ImageToJpgApp
# Supports: Pillow as primary; optional pillow-heif, pyheif, rawpy/imageio for HEIC/RAW.
# Exposes: open_image(path: Path) -> PIL.Image, convert_to_jpg(...), batch_convert(...)

from pathlib import Path
from typing import Optional, Tuple, Iterable, Callable, List
import logging
import io

from PIL import Image, ImageFile, ExifTags

ImageFile.LOAD_TRUNCATED_IMAGES = True
logger = logging.getLogger("ImageToJpgApp.converter")

# --- Optional HEIF/HEIC support via pillow-heif or pyheif ---
HAS_PILLOW_HEIF = False
HAS_PYHEIF = False
try:
    # pillow-heif integrates with Pillow and provides Image.open support
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
    HAS_PILLOW_HEIF = True
    logger.debug("pillow-heif detected and registered")
except Exception:
    HAS_PILLOW_HEIF = False

# pyheif is another option; keep as fallback if available
try:
    import pyheif  # type: ignore
    HAS_PYHEIF = True
    logger.debug("pyheif available (will be used as fallback for HEIF)")
except Exception:
    HAS_PYHEIF = False

# RAW support via rawpy + imageio (optional)
HAS_RAWPY = False
try:
    import rawpy  # type: ignore
    import imageio  # type: ignore
    HAS_RAWPY = True
    logger.debug("rawpy + imageio available for RAW formats")
except Exception:
    HAS_RAWPY = False

# Supported extensions (lowercase)
SUPPORTED_INPUT_EXTS = {
    ".png", ".gif", ".tif", ".tiff", ".psd", ".svg", ".webp", ".heic", ".heif",
    ".raw", ".cr2", ".nef", ".arw", ".dng", ".rw2"
}

# --- Helper functions ---

def _pillow_open(path: Path) -> Image.Image:
    """Open via Pillow; may raise."""
    return Image.open(path)

def _open_heif_pyheif(path: Path) -> Image.Image:
    """Open HEIF/HEIC via pyheif and wrap into Pillow Image."""
    # pyheif returns raw bytes and metadata; convert to PIL.Image
    import pyheif  # local import; already attempted above
    heif_file = pyheif.read(path.read_bytes())
    mode = heif_file.mode
    size = heif_file.size
    data = heif_file.data
    # Some pyheif builds expose stride; use Image.frombytes
    try:
        img = Image.frombytes(mode, size, data, "raw", mode, heif_file.stride)
    except Exception:
        # fallback: use RGBA conversion
        img = Image.frombytes(mode, size, data)
    return img

def _open_raw(path: Path) -> Image.Image:
    """Open RAW files using rawpy + imageio, return PIL Image."""
    import rawpy  # local import
    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess()
    return Image.fromarray(rgb)

def open_image(path: Path) -> Image.Image:
    """
    Open an image with the best available handler.
    - Path: pathlib.Path
    - Returns: PIL.Image
    Raises exception on failure.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    ext = path.suffix.lower()

    # HEIF/HEIC handling
    if ext in {".heic", ".heif"}:
        # Prefer pillow-heif if available (registered into Pillow)
        if HAS_PILLOW_HEIF:
            return _pillow_open(path)
        if HAS_PYHEIF:
            try:
                return _open_heif_pyheif(path)
            except Exception as e:
                logger.debug("pyheif open failed: %s", e)
        # fallback to Pillow (may fail)
        return _pillow_open(path)

    # RAW handling
    if ext in {".raw", ".cr2", ".nef", ".arw", ".dng", ".rw2"}:
        if HAS_RAWPY:
            try:
                return _open_raw(path)
            except Exception as e:
                logger.debug("rawpy open failed: %s", e)
        # fallback to Pillow (may fail)
        return _pillow_open(path)

    # Other formats: defer to Pillow (SVG may require cairosvg or pillow-simd etc)
    return _pillow_open(path)

def _preserve_exif_bytes(src_img: Image.Image) -> Optional[bytes]:
    """Try to extract raw EXIF bytes from a Pillow Image (if any)."""
    try:
        exif = src_img.info.get("exif")
        if isinstance(exif, (bytes, bytearray)):
            return bytes(exif)
    except Exception:
        logger.debug("Failed to read EXIF bytes")
    return None

def _ensure_rgb_for_jpeg(img: Image.Image, background: Tuple[int, int, int]) -> Image.Image:
    """
    Convert image to RGB suitable for JPEG.
    If the image has an alpha channel, composite it onto a single-color background.
    Returns a new Image object (caller should close original if necessary).
    """
    try:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGB", img.size, background)
            if img.mode in ("RGBA", "LA"):
                alpha = img.split()[-1]
                bg.paste(img.convert("RGBA"), mask=alpha)
            else:
                # Palette with transparency
                rgba = img.convert("RGBA")
                alpha = rgba.split()[-1]
                bg.paste(rgba, mask=alpha)
            return bg
        else:
            return img.convert("RGB")
    except Exception:
        # In case conversion fails, fallback to converting directly to RGB
        try:
            return img.convert("RGB")
        except Exception:
            raise

# --- Public API: convert single image to JPEG ---

def convert_to_jpg(
    src_path: str,
    dst_dir: str,
    quality: int = 85,
    background: Tuple[int, int, int] = (255, 255, 255),
    keep_exif: bool = False,
    overwrite: bool = False,
    jpeg_subsample: Optional[str] = None
) -> str:
    """
    Convert a single image to JPEG.

    Args:
      src_path: source file path (str)
      dst_dir: output directory (str)
      quality: JPEG quality (1-95)
      background: RGB tuple used for compositing transparent images
      keep_exif: attempt to preserve EXIF bytes where possible
      overwrite: if True, overwrite existing file; otherwise add numeric suffix to avoid collision
      jpeg_subsample: pass to Pillow 'subsampling' option if desired ('4:4:4', '4:2:0', etc) - None uses default

    Returns:
      The saved JPEG file path (str)

    Raises:
      RuntimeError on failure.
    """
    src = Path(src_path)
    dst_dir = Path(dst_dir)
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {src}")
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Open image with fallback handlers
    try:
        img = open_image(src)
    except Exception as e:
        logger.exception("Failed to open image %s: %s", src, e)
        raise RuntimeError(f"Failed to open {src}: {e}") from e

    # Prepare RGB image for JPEG
    try:
        out_img = _ensure_rgb_for_jpeg(img, background)
    except Exception as e:
        logger.exception("Failed to convert image mode: %s", e)
        # attempt to close and raise
        try:
            img.close()
        except Exception:
            pass
        raise RuntimeError(f"Error converting image mode: {e}") from e

    # Determine destination filename
    base_name = src.stem
    dst_path = dst_dir.joinpath(f"{base_name}.jpg")
    if dst_path.exists() and not overwrite:
        i = 1
        while True:
            candidate = dst_dir.joinpath(f"{base_name}_{i}.jpg")
            if not candidate.exists():
                dst_path = candidate
                break
            i += 1

    # Build save kwargs
    save_kwargs = {"format": "JPEG", "quality": int(max(1, min(95, quality))), "optimize": True}
    if jpeg_subsample is not None:
        save_kwargs["subsampling"] = jpeg_subsample  # Pillow accepts 'subsampling' in some versions

    # Preserve EXIF if requested and available
    if keep_exif:
        exif_bytes = _preserve_exif_bytes(img)
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes

    try:
        out_img.save(dst_path, **save_kwargs)
        saved = str(dst_path)
        logger.info("Saved JPEG: %s", saved)
    except Exception as e:
        logger.exception("Failed to save JPEG %s: %s", dst_path, e)
        raise RuntimeError(f"Failed to save JPEG to {dst_path}: {e}") from e
    finally:
        # Close images where appropriate
        try:
            img.close()
        except Exception:
            pass
        try:
            if out_img is not img:
                out_img.close()
        except Exception:
            pass

    return saved

# --- Batch conversion helper with progress callback ---

def batch_convert(
    src_paths: Iterable[str],
    dst_dir: str,
    quality: int = 85,
    background: Tuple[int, int, int] = (255, 255, 255),
    keep_exif: bool = False,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[int, int, str, str, Optional[str]], None]] = None
) -> List[Tuple[str, str, Optional[str]]]:
    """
    Convert multiple images sequentially (caller can run in threadpool).
    progress_callback signature: (index:int, total:int, src:str, dst:str, error:Optional[str])

    Returns list of tuples (src, dst_or_empty, error_or_None)
    """
    src_list = list(src_paths)
    total = len(src_list)
    results: List[Tuple[str, str, Optional[str]]] = []
    for idx, src in enumerate(src_list, start=1):
        dst = ""
        err = None
        try:
            saved = convert_to_jpg(
                src_path=src,
                dst_dir=dst_dir,
                quality=quality,
                background=background,
                keep_exif=keep_exif,
                overwrite=overwrite
            )
            dst = saved
        except Exception as e:
            logger.exception("batch_convert error for %s: %s", src, e)
            err = str(e)
        results.append((src, dst, err))
        if callable(progress_callback):
            try:
                progress_callback(idx, total, src, dst, err)
            except Exception:
                logger.exception("progress_callback raised an exception")
    return results

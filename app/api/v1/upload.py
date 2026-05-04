"""Generic file upload endpoint — images and PDFs."""
import io
import os
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter(prefix="/upload")

_ALLOWED_IMAGE = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
_ALLOWED_PDF = {"application/pdf"}


@router.post("")
async def upload_file(file: UploadFile = File(...)):
    """Upload an image or PDF. Returns { url, file_name, type }."""
    from app.core.config import get_settings

    settings = get_settings()
    use_s3 = bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)

    content_type = file.content_type or ""
    is_image = content_type in _ALLOWED_IMAGE
    is_pdf = content_type in _ALLOWED_PDF

    if not is_image and not is_pdf:
        # Fallback: detect by extension
        fname = (file.filename or "").lower()
        if fname.endswith(".pdf"):
            is_pdf = True
        elif any(fname.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            is_image = True
        else:
            raise HTTPException(status_code=400, detail="Only images and PDFs are allowed")

    content = await file.read()
    asset_id = str(uuid.uuid4())
    original_name = file.filename or ("file.pdf" if is_pdf else "image.jpg")

    if is_pdf:
        if use_s3:
            import boto3
            s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_S3_REGION,
            )
            bucket = settings.AWS_S3_BUCKET
            cdn = settings.CDN_BASE_URL.rstrip("/") if settings.CDN_BASE_URL else f"https://{bucket}.s3.amazonaws.com"
            key = f"uploads/{asset_id}/{original_name}"
            s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType="application/pdf")
            url = f"{cdn}/{key}"
        else:
            local_dir = f"/app/media/uploads/{asset_id}"
            os.makedirs(local_dir, exist_ok=True)
            local_path = f"{local_dir}/{original_name}"
            with open(local_path, "wb") as fout:
                fout.write(content)
            url = f"/media/uploads/{asset_id}/{original_name}"
        return {"url": url, "file_name": original_name, "type": "pdf"}

    # Image: resize and convert to JPEG/WebP
    from PIL import Image as PILImage

    img = PILImage.open(io.BytesIO(content)).convert("RGB")
    img.thumbnail((800, 800), PILImage.LANCZOS)
    base_name = original_name.rsplit(".", 1)[0]

    if use_s3:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        bucket = settings.AWS_S3_BUCKET
        cdn = settings.CDN_BASE_URL.rstrip("/") if settings.CDN_BASE_URL else f"https://{bucket}.s3.amazonaws.com"
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        key = f"uploads/{asset_id}/{base_name}.jpg"
        s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue(), ContentType="image/jpeg")
        url = f"{cdn}/{key}"
    else:
        local_dir = f"/app/media/uploads/{asset_id}"
        os.makedirs(local_dir, exist_ok=True)
        out_path = f"{local_dir}/{base_name}.jpg"
        img.save(out_path, "JPEG", quality=85, optimize=True)
        url = f"/media/uploads/{asset_id}/{base_name}.jpg"

    return {"url": url, "file_name": f"{base_name}.jpg", "type": "image"}

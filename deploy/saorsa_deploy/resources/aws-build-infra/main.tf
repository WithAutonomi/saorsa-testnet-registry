resource "aws_s3_bucket" "builds" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "builds" {
  bucket = aws_s3_bucket.builds.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "builds_public_read" {
  bucket = aws_s3_bucket.builds.id

  depends_on = [aws_s3_bucket_public_access_block.builds]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.builds.arn}/*"
      },
    ]
  })
}

resource "aws_iam_user" "build_uploader" {
  name = "saorsa-build-uploader"
}

resource "aws_iam_user_policy" "build_uploader" {
  name = "saorsa-build-upload-policy"
  user = aws_iam_user.build_uploader.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "s3:PutObject"
        Resource = "${aws_s3_bucket.builds.arn}/*"
      },
    ]
  })
}

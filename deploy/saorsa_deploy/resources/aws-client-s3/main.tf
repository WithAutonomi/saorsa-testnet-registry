# Public bucket for testnet address lists. Only content addresses live here —
# nothing sensitive — and public read lets downloaders list + fetch with
# unsigned HTTPS, which keeps working on clock-skewed client VMs whose own
# SigV4-signed requests are rejected (~15min tolerance). Deliberately a
# separate bucket from the tfstate bucket, which keeps full Block Public
# Access intact. Writes stay private: uploaders flush through presigned PUT
# URLs minted at provision time with the IAM user below.
resource "aws_s3_bucket" "addresses" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "addresses" {
  bucket = aws_s3_bucket.addresses.id

  # ACLs stay blocked; public access is granted via the bucket policy only.
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "addresses_public_read" {
  bucket     = aws_s3_bucket.addresses.id
  depends_on = [aws_s3_bucket_public_access_block.addresses]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadAddressLists"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:aws:s3:::${var.bucket_name}/*"
      },
      {
        Sid       = "PublicListAddressLists"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:ListBucket"
        Resource  = "arn:aws:s3:::${var.bucket_name}"
      },
    ]
  })
}

resource "aws_iam_user" "client_s3" {
  name = var.iam_user_name
}

resource "aws_iam_user_policy" "client_s3" {
  name = "${var.iam_user_name}-policy"
  user = aws_iam_user.client_s3.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucketForAddressesPrefix"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::${var.bucket_name}"
        Condition = {
          StringLike = {
            "s3:prefix" = ["${var.addresses_prefix}/*"]
          }
        }
      },
      {
        Sid    = "ObjectsUnderAddressesPrefix"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "arn:aws:s3:::${var.bucket_name}/${var.addresses_prefix}/*"
      },
    ]
  })
}

resource "aws_iam_access_key" "client_s3" {
  user = aws_iam_user.client_s3.name
}

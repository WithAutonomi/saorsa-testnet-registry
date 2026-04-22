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

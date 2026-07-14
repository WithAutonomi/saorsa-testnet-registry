variable "name" {
  type        = string
  description = "Deployment name (prefix for VM/secret names)."
}

variable "region" {
  type        = string
  default     = "EU"
  description = "Contabo region code (EU, US-central, US-east, US-west, UK, SIN, AUS, JPN, IND)."
}

variable "product_id" {
  type        = string
  default     = "V45"
  description = "Contabo product id for the Elasticsearch VM. Verify current ids via `cntb get products`."
}

variable "image_id" {
  type        = string
  default     = ""
  description = <<EOT
UUID of the Ubuntu image. Resolve via `cntb get images`. Required on create (the
saorsa-deploy CLI validates it is non-empty); defaults to "" so `terraform
destroy` does not require it.
EOT
}

variable "ssh_public_key_path" {
  type        = string
  default     = "~/.ssh/id_rsa.pub"
  description = "Operator SSH public key path; registered as a contabo_secret and authorized for root."
}

variable "period" {
  type        = number
  default     = 1
  description = "Initial contract period in months (1, 3, 6, 12)."
}

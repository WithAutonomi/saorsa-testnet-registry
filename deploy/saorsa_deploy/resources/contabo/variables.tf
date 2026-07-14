variable "name" {
  type        = string
  description = "Deployment name (used as the prefix for VM/secret names)."
}

variable "region" {
  type        = string
  default     = "EU"
  description = <<EOT
Contabo region code. Known values: EU, US-central, US-east, US-west, UK, SIN,
AUS, JPN, IND (the Terraform provider docs list only the first five, but the
create API accepts the wider set). Availability of a given product varies per
region — verify with `cntb get products`.
EOT
}

variable "vm_count" {
  type        = number
  description = "Number of instances to create in this region."
}

variable "product_id" {
  type        = string
  default     = "V92"
  description = <<EOT
Contabo product id (the instance size/plan). Default V92 (VPS 20 SSD, ~200 GB).
VDS ids (V8-V16) offer far more disk for a permanent debug node. Product ids
drift from the docs — verify current ids via `cntb get products --productType vps`
(or vds) or the createInstance API.
EOT
}

variable "image_id" {
  type        = string
  default     = ""
  description = <<EOT
UUID of the OS image (e.g. Ubuntu 24.04). Contabo standard images have stable
UUIDs but they are NOT discoverable by name via Terraform — resolve the current
UUID with `cntb get images` (or the Images API) and pass it in. Required on
create (the saorsa-deploy CLI validates it is non-empty); defaults to "" so that
`terraform destroy` — which only needs the recorded state — does not require it.
EOT
}

variable "ssh_public_key_path" {
  type        = string
  default     = "~/.ssh/id_rsa.pub"
  description = <<EOT
Path to the operator's SSH PUBLIC key. Its contents are registered as a
contabo_secret (type ssh) and authorized for the root user on every instance.
~ is expanded. Must correspond to the private key passed to the provisioner via
--ssh-key-path.
EOT
}

variable "period" {
  type        = number
  default     = 1
  description = <<EOT
Initial contract period in months (Contabo billing is a committed prepaid term,
not hourly). Allowed: 1, 3, 6, 12. Longer terms are cheaper but lock in.
EOT
}

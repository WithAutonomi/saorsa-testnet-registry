variable "service_name" {
  type        = string
  description = "OVH Public Cloud project ID (UUID-shaped string)."
}

variable "name" {
  type        = string
  description = "Deployment name (used as the prefix for VM and volume names)."
}

variable "region" {
  type        = string
  description = "OVH region (e.g. GRA9, SBG5, DE1, UK1, WAW1, BHS5)."
}

variable "flavor_name" {
  type        = string
  default     = "b3-8"
  description = "OVH flavor for the Elasticsearch VM (e.g. b3-8 for 8 vCPU / 32 GB)."
}

variable "attached_volume_size" {
  type        = number
  default     = 100
  description = "Size in GB for the Elasticsearch data volume."
}

variable "volume_type" {
  type        = string
  default     = "high-speed"
  description = <<EOT
OVH block storage type for the Elasticsearch data volume. Allowed: classic,
classic-luks, classic-multiattach, high-speed, high-speed-luks,
high-speed-gen2, high-speed-gen2-luks. Default high-speed (SSD) — ES indexing
and search benefit from fast disk.
EOT
}

variable "ssh_key_name" {
  type        = string
  default     = "ant_deploy"
  description = "Name of the SSH key already registered in the OVH project."
}

variable "ubuntu_image_name_pattern" {
  type        = string
  default     = "Ubuntu 24.04"
  description = "Substring matched against image names to pick the Ubuntu image."
}

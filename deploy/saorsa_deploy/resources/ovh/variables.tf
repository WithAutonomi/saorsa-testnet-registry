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
  description = "OVH region (e.g. BHS5, GRA9, DE1, UK1, WAW1)."
}

variable "vm_count" {
  type        = number
  description = "Number of VMs to create in this region."
}

variable "flavor_name" {
  type        = string
  default     = "b3-16"
  description = "OVH flavor name (e.g. b3-16 for 4 vCPU / 16 GB)."
}

variable "attached_volume_size" {
  type        = number
  default     = 50
  description = "Size in GB for the attached data volume on each VM."
}

variable "ssh_key_name" {
  type        = string
  default     = "ant_deploy"
  description = "Name of the SSH key already registered in the OVH project."
}

variable "name_template" {
  type        = string
  default     = "ovh"
  description = <<EOT
Infix used between the deployment name and the region in resource names.
Defaults to "ovh" (e.g. ant-prod-01-ovh-BHS5-1).
EOT
}

variable "name_index_offset" {
  type        = number
  default     = 0
  description = <<EOT
Added to count.index when generating names so a vm_count of 3 with offset 1
produces -<region>-2, -3, -4 instead of -1, -2, -3. Used by the migration
command to avoid colliding with existing prod VM names in the inventory.
EOT
}

variable "ubuntu_image_name_pattern" {
  type        = string
  default     = "Ubuntu 24.04"
  description = "Substring matched against image names to pick the Ubuntu image."
}

variable "volume_type" {
  type        = string
  default     = "high-speed"
  description = <<EOT
OVH block storage type. Allowed: classic, classic-luks, classic-multiattach,
high-speed, high-speed-luks, high-speed-gen2, high-speed-gen2-luks.
EOT
}

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
  description = "OVH flavor for the InfluxDB VM (e.g. b3-8 for 8 vCPU / 32 GB)."
}

variable "grafana_flavor_name" {
  type        = string
  default     = "d2-4"
  description = <<EOT
OVH flavor for the (small) Grafana VM. Grafana only renders panels, so a
modest flavor (default d2-4: 2 vCPU / 4 GB) is plenty.
EOT
}

variable "attached_volume_size" {
  type        = number
  default     = 50
  description = "Size in GB for the InfluxDB data volume attached to the InfluxDB VM."
}

variable "volume_type" {
  type        = string
  default     = "high-speed"
  description = <<EOT
OVH block storage type for the InfluxDB data volume. Allowed: classic,
classic-luks, classic-multiattach, high-speed, high-speed-luks,
high-speed-gen2, high-speed-gen2-luks. Default high-speed (SSD) — InfluxDB
query/render performance benefits from fast disk.
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

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
  description = "OVH 3-AZ region (EU-WEST-PAR or EU-SOUTH-MIL)."
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

variable "ssh_public_key_path" {
  type        = string
  default     = "~/.ssh/ant_deploy.pub"
  description = <<EOT
Path to the operator's SSH public key. A per-deployment Nova keypair is created
from its contents (OVH does not reliably expose panel SSH keys as Nova keypairs).
Defaults to the standard ant_deploy key so no extra plumbing is needed; ~ is
expanded. Mirrors how resources/ovh hardcodes the ant_deploy key name.
EOT
}

variable "availability_zones" {
  type        = list(string)
  default     = []
  description = <<EOT
Availability zones to round-robin the region's VMs across, e.g.
["eu-west-par-a","eu-west-par-b","eu-west-par-c"]. Empty lets Nova auto-place.
Applied to the INSTANCE only; the volume is regional (see volume_type).
EOT
}

variable "public_network_name" {
  type        = string
  default     = "Ext-Net"
  description = "OVH OpenStack public network name to attach the instance to."
}

variable "name_template" {
  type        = string
  default     = "ovh"
  description = "Infix between the deployment name and region in resource names."
}

variable "name_index_offset" {
  type        = number
  default     = 0
  description = "Added to count.index when generating names (matches resources/ovh)."
}

variable "ubuntu_image_name_pattern" {
  type        = string
  default     = "Ubuntu 24.04"
  description = "Substring matched against image names to pick the Ubuntu image."
}

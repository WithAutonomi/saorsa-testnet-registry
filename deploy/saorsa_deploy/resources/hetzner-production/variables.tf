variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "location" {
  type        = string
  description = "Hetzner location (e.g. fsn1, ash)"
}

variable "floating_ip_id" {
  type        = number
  description = "ID of the pre-allocated Hetzner floating IP"
}

variable "volume_size" {
  type        = number
  default     = 50
  description = "Size in GB for each of the two striped volumes"
}

variable "ssh_key_id" {
  type        = string
  description = "ID of pre-registered SSH key"
}

variable "server_type" {
  type        = string
  default     = "cpx21"
  description = "Hetzner server type (e.g. cpx21, cpx22)"
}

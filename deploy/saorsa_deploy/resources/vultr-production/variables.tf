variable "vultr_api_key" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "Vultr region ID (e.g. nrt, syd)"
}

variable "reserved_ip_id" {
  type        = string
  description = "ID of the pre-allocated Vultr reserved IP"
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

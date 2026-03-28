variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "AWS region (e.g. sa-east-1)"
}

variable "eip_allocation_id" {
  type        = string
  description = "Allocation ID of the pre-allocated Elastic IP"
}

variable "volume_size" {
  type        = number
  default     = 50
  description = "Size in GB for each of the two striped volumes"
}

variable "key_pair_name" {
  type        = string
  description = "Name of pre-registered EC2 key pair"
}

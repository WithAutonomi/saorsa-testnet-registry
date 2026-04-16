variable "do_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "client_index" {
  type        = number
  description = "Index of this client VM (used in naming)"
}

variable "droplet_size" {
  type        = string
  default     = "s-2vcpu-4gb"
  description = "Droplet size slug (e.g. s-2vcpu-4gb, s-4vcpu-8gb)"
}

variable "ssh_key_ids" {
  type = list(number)
  default = [
    36971688, # David Irvine
    30643816, # Anselme Grumbach
    30113222, # Qi Ma
    42022675, # Shu
    30878672, # Chris O'Neil
    31216015, # QA
    34183228, # GH Actions Automation
    38596814, # sn-testnet-workflows automation
    54385801  # saorsa-deploy
  ]
}

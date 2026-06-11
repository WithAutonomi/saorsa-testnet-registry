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

variable "droplet_size" {
  type    = string
  default = "s-4vcpu-8gb"
}

variable "grafana_droplet_size" {
  type    = string
  default = "s-1vcpu-2gb"
}

variable "enable_grafana" {
  type        = bool
  default     = true
  description = <<EOT
Whether to create the separate Grafana VM. Defaults to true so direct
terraform usage is unchanged; saorsa-deploy passes an explicit value (off by
default — opt in with `monitoring infra --grafana`).
EOT
}

variable "volume_size" {
  type    = number
  default = 50
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

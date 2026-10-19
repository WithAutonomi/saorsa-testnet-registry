variable "hcloud_token" {
  type      = string
  sensitive = true
}

variable "name" {
  type = string
}

variable "region" {
  type        = string
  description = "Hetzner location (e.g. fsn1, nbg1, hel1, ash)"
}

variable "role" {
  type        = string
  description = "Client role: 'upload', 'download', 'wasm-upload', 'wasm-download', 'pointer' or 'browser-download'"
  validation {
    # The wasm-* roles run a Node.js client driving ant-core compiled to WASM
    # over WebRTC-Direct. They are distinct roles rather than a flag on the
    # native ones because the role names the VM, its Terraform workspace and its
    # S3 state key -- telling the two transports apart is the point.
    # `pointer` drives `ant pointer create|update|get|resolve` for the life of
    # the run. Also its own role: a pointer client pays for every write from its
    # own wallet and publishes its own address list, so it needs its own VM name,
    # workspace and state key.
    # `browser-download` runs N headless Chromium instances through the harness
    # in `resources/browser-client/`, all reading one chosen hot file on a shared
    # wall-clock schedule, and takes no wallet. Its own role for the same reason
    # again -- the role names the VM, its Terraform workspace and its S3 state
    # key -- and because `clients clean` and `clients destroy` dispatch on it.
    condition     = contains(["upload", "download", "wasm-upload", "wasm-download", "pointer", "browser-download"], var.role)
    error_message = "role must be one of 'upload', 'download', 'wasm-upload', 'wasm-download', 'pointer', 'browser-download'."
  }
}

variable "client_index" {
  type        = number
  description = "Index of this client VM (used in naming)"
}

variable "server_type" {
  type        = string
  default     = "cpx22"
  description = "Hetzner server type (e.g. cx22, cax21, cpx22)"
}

variable "ssh_key_ids" {
  type        = list(string)
  description = "IDs of pre-registered SSH keys in Hetzner"
  default = [
    "109855983", # ant-production
    "109945191", # ant-deploy
    "110444940", # testnet-key
  ]
}

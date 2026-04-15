output "vm_ips" {
  value = vultr_instance.node_vm[*].main_ip
}

output "vm_ids" {
  value = vultr_instance.node_vm[*].id
}

output "volume_ids" {
  value = vultr_block_storage.node_storage[*].id
}

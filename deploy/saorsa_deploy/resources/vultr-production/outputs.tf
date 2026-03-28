output "instance_ip" {
  value = vultr_instance.production_node.main_ip
}

output "instance_id" {
  value = vultr_instance.production_node.id
}

output "volume_a_id" {
  value = vultr_block_storage.stripe_a.id
}

output "volume_b_id" {
  value = vultr_block_storage.stripe_b.id
}

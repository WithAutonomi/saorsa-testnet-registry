output "instance_ip" {
  value = aws_instance.production_node.public_ip
}

output "instance_id" {
  value = aws_instance.production_node.id
}

output "volume_a_id" {
  value = aws_ebs_volume.stripe_a.id
}

output "volume_b_id" {
  value = aws_ebs_volume.stripe_b.id
}

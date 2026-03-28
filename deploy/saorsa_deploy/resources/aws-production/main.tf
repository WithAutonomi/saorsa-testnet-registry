data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_vpc" "production" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_subnet" "production" {
  vpc_id                  = aws_vpc.production.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_internet_gateway" "production" {
  vpc_id = aws_vpc.production.id

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_route_table" "production" {
  vpc_id = aws_vpc.production.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.production.id
  }

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_route_table_association" "production" {
  subnet_id      = aws_subnet.production.id
  route_table_id = aws_route_table.production.id
}

resource "aws_security_group" "production_node" {
  name        = "ant-bootstrap-${var.region}"
  description = "Autonomi production bootstrap node"
  vpc_id      = aws_vpc.production.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Autonomi node UDP"
    from_port   = 10000
    to_port     = 10000
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Autonomi node TCP"
    from_port   = 10000
    to_port     = 10000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_instance" "production_node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.medium"
  key_name               = var.key_pair_name
  subnet_id              = aws_subnet.production.id
  vpc_security_group_ids = [aws_security_group.production_node.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  tags = {
    Name = "ant-bootstrap-${var.region}"
  }
}

resource "aws_eip_association" "production_ip" {
  instance_id   = aws_instance.production_node.id
  allocation_id = var.eip_allocation_id
}

resource "aws_ebs_volume" "stripe_a" {
  availability_zone = aws_instance.production_node.availability_zone
  size              = var.volume_size
  type              = "gp3"

  tags = {
    Name = "ant-bootstrap-stripe-a-${var.region}"
  }
}

resource "aws_ebs_volume" "stripe_b" {
  availability_zone = aws_instance.production_node.availability_zone
  size              = var.volume_size
  type              = "gp3"

  tags = {
    Name = "ant-bootstrap-stripe-b-${var.region}"
  }
}

resource "aws_volume_attachment" "stripe_a_attach" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.stripe_a.id
  instance_id = aws_instance.production_node.id
}

resource "aws_volume_attachment" "stripe_b_attach" {
  device_name = "/dev/xvdg"
  volume_id   = aws_ebs_volume.stripe_b.id
  instance_id = aws_instance.production_node.id
}

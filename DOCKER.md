# Docker Build Guide

This guide explains how to build and run Freqtrade using Docker with the provided Makefile.

## Quick Start

```bash
# Show all available commands
make help

# Build the Docker image
make build

# Build and run immediately
make quick

# Run with bash shell for debugging
make run-bash
```

## Common Commands

### Building

```bash
make build              # Build with version tag
make build-latest       # Build and tag as latest
make build-no-cache     # Build without cache
make rebuild           # Clean and rebuild
```

### Running

```bash
make run               # Run interactively
make run-bash          # Run with bash shell
make run-local         # Run with local user_data mounted
make run-config CONFIG=config.json  # Run with specific config
```

### Management

```bash
make info              # Show image info
make clean             # Remove images
make clean-all         # Remove all freqtrade images/containers
make test              # Test the built image
```

## Advanced Usage

### Development

```bash
make build-dev         # Build development image
make dev-shell         # Start development shell
make dev-install       # Install dev dependencies locally
```

### Registry Operations

```bash
# Push to registry
make push DOCKER_REGISTRY=your-registry.com

# Pull from registry  
make pull DOCKER_REGISTRY=your-registry.com
```

### Multi-platform Builds

```bash
make build-multi       # Build for multiple architectures
```

### Custom Build Args

```bash
make build-arg ARGS="--build-arg KEY=VALUE"
```

## Configuration

The Makefile uses these variables (can be overridden):

- `PROJECT_NAME`: freqtrade
- `VERSION`: Auto-detected from code
- `DOCKER_REGISTRY`: Your registry URL
- `PLATFORM`: linux/amd64,linux/arm64

## Examples

### Basic Usage

```bash
# 1. Build the image
make build

# 2. Create user_data directory
mkdir -p user_data

# 3. Run with local data mounted
make run-local

# 4. Or run a specific command
docker run --rm freqtrade:dev --help
```

### Production Setup

```bash
# 1. Build optimized image
make build-latest

# 2. Run with your config
make run-config CONFIG=user_data/config.json

# 3. Or run in background
docker run -d --name freqtrade-bot \
  -v $(pwd)/user_data:/freqtrade/user_data \
  freqtrade:latest trade -c user_data/config.json
```

### Development Workflow

```bash
# 1. Build dev image
make build-dev

# 2. Start development shell
make dev-shell

# 3. Inside container, run commands
freqtrade create-userdir --userdir user_data
freqtrade new-strategy --strategy MyStrategy
freqtrade backtesting --strategy MyStrategy
```

### Registry Workflow

```bash
# 1. Build and push
make push DOCKER_REGISTRY=docker.io/myuser

# 2. On another machine, pull and run
make pull DOCKER_REGISTRY=docker.io/myuser
docker run docker.io/myuser/freqtrade:latest --help
```

## Troubleshooting

### Common Issues

**Build fails with dependencies:**
```bash
make build-no-cache    # Force rebuild without cache
```

**Permission issues:**
```bash
sudo chown -R $(id -u):$(id -g) user_data
```

**Image too large:**
```bash
make size              # Check image size
make prune             # Clean up Docker system
```

### Debugging

```bash
# Check container logs
make logs

# Get shell access to running container
docker exec -it <container-id> /bin/bash

# Inspect image layers
make size
```

## File Structure

```
freqtrade/
├── Dockerfile         # Multi-stage Docker build
├── Makefile          # Build automation
├── DOCKER.md         # This guide
├── requirements*.txt # Python dependencies
└── user_data/        # Your trading data (mount point)
```

## Next Steps

1. **Configure**: Create your `user_data/config.json`
2. **Strategy**: Develop your trading strategy
3. **Backtest**: Test with historical data
4. **Deploy**: Run in production with proper monitoring

For more details, see the main Freqtrade documentation.
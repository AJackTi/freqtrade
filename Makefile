# Freqtrade Docker Build Makefile
# ================================

# Variables
PROJECT_NAME := freqtrade
VERSION := $(shell python -c "import freqtrade; print(freqtrade.__version__)" 2>/dev/null || echo "dev")
IMAGE_NAME := $(PROJECT_NAME):$(VERSION)
IMAGE_LATEST := $(PROJECT_NAME):latest
DOCKERFILE := Dockerfile
DOCKER_REGISTRY ?= 
BUILD_ARGS := 
PLATFORM := linux/amd64,linux/arm64

# Colors for output
RED := \033[31m
GREEN := \033[32m
YELLOW := \033[33m
BLUE := \033[34m
RESET := \033[0m

# Default target
.DEFAULT_GOAL := help

# Help target
.PHONY: help
help: ## Show this help message
	@echo "$(BLUE)Freqtrade Docker Build Commands$(RESET)"
	@echo "================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)Variables:$(RESET)"
	@echo "  PROJECT_NAME    = $(PROJECT_NAME)"
	@echo "  VERSION         = $(VERSION)"
	@echo "  IMAGE_NAME      = $(IMAGE_NAME)"
	@echo "  DOCKER_REGISTRY = $(DOCKER_REGISTRY)"
	@echo "  PLATFORM        = $(PLATFORM)"

# Build targets
.PHONY: build
build: ## Build Docker image with version tag
	@echo "$(BLUE)Building Docker image: $(IMAGE_NAME)$(RESET)"
	docker build $(BUILD_ARGS) -t $(IMAGE_NAME) -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Build complete: $(IMAGE_NAME)$(RESET)"

.PHONY: build-latest
build-latest: build ## Build and tag as latest
	@echo "$(BLUE)Tagging as latest: $(IMAGE_LATEST)$(RESET)"
	docker tag $(IMAGE_NAME) $(IMAGE_LATEST)
	@echo "$(GREEN)✅ Tagged as latest$(RESET)"

.PHONY: build-no-cache
build-no-cache: ## Build without using cache
	@echo "$(BLUE)Building without cache: $(IMAGE_NAME)$(RESET)"
	docker build --no-cache $(BUILD_ARGS) -t $(IMAGE_NAME) -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Build complete (no cache): $(IMAGE_NAME)$(RESET)"

.PHONY: build-multi
build-multi: ## Build multi-platform image
	@echo "$(BLUE)Building multi-platform image: $(IMAGE_NAME)$(RESET)"
	docker buildx build --platform $(PLATFORM) $(BUILD_ARGS) -t $(IMAGE_NAME) -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Multi-platform build complete: $(IMAGE_NAME)$(RESET)"

.PHONY: build-dev
build-dev: ## Build development image with dev requirements
	@echo "$(BLUE)Building development image$(RESET)"
	docker build --target python-deps $(BUILD_ARGS) -t $(PROJECT_NAME):dev -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Development image built: $(PROJECT_NAME):dev$(RESET)"

# Run targets
.PHONY: run
run: ## Run the container interactively
	@echo "$(BLUE)Running container: $(IMAGE_NAME)$(RESET)"
	docker run -it --rm $(IMAGE_NAME)

.PHONY: run-bash
run-bash: ## Run container with bash shell
	@echo "$(BLUE)Running container with bash: $(IMAGE_NAME)$(RESET)"
	docker run -it --rm --entrypoint /bin/bash $(IMAGE_NAME)

.PHONY: run-local
run-local: ## Run with local user_data mounted
	@echo "$(BLUE)Running with local user_data mounted$(RESET)"
	@mkdir -p user_data
	docker run -it --rm -v $(PWD)/user_data:/freqtrade/user_data $(IMAGE_NAME)

.PHONY: run-config
run-config: ## Run with custom config (CONFIG=path/to/config.json)
ifndef CONFIG
	@echo "$(RED)❌ CONFIG variable not set. Use: make run-config CONFIG=path/to/config.json$(RESET)"
	@exit 1
endif
	@echo "$(BLUE)Running with config: $(CONFIG)$(RESET)"
	docker run -it --rm -v $(PWD)/$(CONFIG):/freqtrade/config.json $(IMAGE_NAME) trade -c config.json

# Management targets
.PHONY: clean
clean: ## Remove built images
	@echo "$(YELLOW)Removing images...$(RESET)"
	-docker rmi $(IMAGE_NAME) $(IMAGE_LATEST) $(PROJECT_NAME):dev 2>/dev/null || true
	@echo "$(GREEN)✅ Images removed$(RESET)"

.PHONY: clean-all
clean-all: ## Remove all freqtrade images and containers
	@echo "$(YELLOW)Removing all freqtrade images and containers...$(RESET)"
	-docker ps -a --filter ancestor=$(PROJECT_NAME) -q | xargs docker rm -f 2>/dev/null || true
	-docker images $(PROJECT_NAME) -q | xargs docker rmi -f 2>/dev/null || true
	@echo "$(GREEN)✅ All freqtrade images and containers removed$(RESET)"

.PHONY: prune
prune: ## Clean up Docker system
	@echo "$(YELLOW)Pruning Docker system...$(RESET)"
	docker system prune -f
	@echo "$(GREEN)✅ Docker system pruned$(RESET)"

# Registry targets
.PHONY: push
push: build-latest ## Push image to registry
ifndef DOCKER_REGISTRY
	@echo "$(RED)❌ DOCKER_REGISTRY not set. Use: make push DOCKER_REGISTRY=your-registry.com$(RESET)"
	@exit 1
endif
	@echo "$(BLUE)Pushing to registry: $(DOCKER_REGISTRY)/$(IMAGE_NAME)$(RESET)"
	docker tag $(IMAGE_NAME) $(DOCKER_REGISTRY)/$(IMAGE_NAME)
	docker tag $(IMAGE_LATEST) $(DOCKER_REGISTRY)/$(IMAGE_LATEST)
	docker push $(DOCKER_REGISTRY)/$(IMAGE_NAME)
	docker push $(DOCKER_REGISTRY)/$(IMAGE_LATEST)
	@echo "$(GREEN)✅ Images pushed to registry$(RESET)"

.PHONY: pull
pull: ## Pull image from registry
ifndef DOCKER_REGISTRY
	@echo "$(RED)❌ DOCKER_REGISTRY not set. Use: make pull DOCKER_REGISTRY=your-registry.com$(RESET)"
	@exit 1
endif
	@echo "$(BLUE)Pulling from registry: $(DOCKER_REGISTRY)/$(IMAGE_LATEST)$(RESET)"
	docker pull $(DOCKER_REGISTRY)/$(IMAGE_LATEST)
	docker tag $(DOCKER_REGISTRY)/$(IMAGE_LATEST) $(IMAGE_LATEST)
	@echo "$(GREEN)✅ Image pulled from registry$(RESET)"

# Info targets
.PHONY: info
info: ## Show Docker image information
	@echo "$(BLUE)Docker Image Information$(RESET)"
	@echo "========================="
	@echo "$(YELLOW)Available Images:$(RESET)"
	@docker images $(PROJECT_NAME) --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}\t{{.Size}}" 2>/dev/null || echo "No images found"
	@echo ""
	@echo "$(YELLOW)Running Containers:$(RESET)"
	@docker ps --filter ancestor=$(PROJECT_NAME) --format "table {{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}" 2>/dev/null || echo "No containers running"

.PHONY: logs
logs: ## Show logs from running container
	@echo "$(BLUE)Container Logs:$(RESET)"
	@docker ps --filter ancestor=$(PROJECT_NAME) -q | head -1 | xargs docker logs -f 2>/dev/null || echo "No running containers found"

.PHONY: size
size: ## Show image size breakdown
	@echo "$(BLUE)Image Size Analysis:$(RESET)"
	@docker images $(PROJECT_NAME) --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
	@echo ""
	@echo "$(YELLOW)Layer History (latest):$(RESET)"
	@docker history $(IMAGE_LATEST) 2>/dev/null || echo "Latest image not found"

# Testing targets
.PHONY: test
test: build ## Build and test the image
	@echo "$(BLUE)Testing Docker image...$(RESET)"
	docker run --rm $(IMAGE_NAME) --version
	docker run --rm $(IMAGE_NAME) --help
	@echo "$(GREEN)✅ Image tests passed$(RESET)"

.PHONY: test-all
test-all: build-latest test ## Build latest and run all tests
	@echo "$(BLUE)Running comprehensive tests...$(RESET)"
	docker run --rm $(IMAGE_LATEST) list-exchanges
	docker run --rm $(IMAGE_LATEST) list-strategies
	@echo "$(GREEN)✅ All tests passed$(RESET)"

# Development targets  
.PHONY: dev-shell
dev-shell: build-dev ## Start development shell
	@echo "$(BLUE)Starting development shell...$(RESET)"
	docker run -it --rm -v $(PWD):/freqtrade $(PROJECT_NAME):dev /bin/bash

.PHONY: dev-install
dev-install: ## Install development dependencies locally
	@echo "$(BLUE)Installing development dependencies...$(RESET)"
	pip install -r requirements-dev.txt
	pip install -e .
	@echo "$(GREEN)✅ Development setup complete$(RESET)"

# Quick commands
.PHONY: quick
quick: build run ## Quick build and run

.PHONY: rebuild
rebuild: clean build ## Clean and rebuild

.PHONY: status
status: info ## Alias for info

# Multi-stage build targets
.PHONY: build-base
build-base: ## Build only base stage
	@echo "$(BLUE)Building base stage...$(RESET)"
	docker build --target base -t $(PROJECT_NAME):base -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Base stage built$(RESET)"

.PHONY: build-deps
build-deps: ## Build only dependencies stage  
	@echo "$(BLUE)Building dependencies stage...$(RESET)"
	docker build --target python-deps -t $(PROJECT_NAME):deps -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Dependencies stage built$(RESET)"

# Utility targets
.PHONY: version
version: ## Show version information
	@echo "$(BLUE)Version Information:$(RESET)"
	@echo "Project: $(PROJECT_NAME)"
	@echo "Version: $(VERSION)"
	@echo "Image: $(IMAGE_NAME)"

.PHONY: env
env: ## Show environment variables
	@echo "$(BLUE)Environment Variables:$(RESET)"
	@env | grep -E "(DOCKER|PROJECT|VERSION)" || true

# Advanced targets
.PHONY: build-arg
build-arg: ## Build with custom build args (ARGS="--build-arg KEY=VALUE")
	@echo "$(BLUE)Building with custom args: $(ARGS)$(RESET)"
	docker build $(ARGS) -t $(IMAGE_NAME) -f $(DOCKERFILE) .
	@echo "$(GREEN)✅ Build complete with custom args$(RESET)"

.PHONY: export
export: build ## Export image to tar file
	@echo "$(BLUE)Exporting image to tar...$(RESET)"
	docker save $(IMAGE_NAME) | gzip > $(PROJECT_NAME)-$(VERSION).tar.gz
	@echo "$(GREEN)✅ Image exported to $(PROJECT_NAME)-$(VERSION).tar.gz$(RESET)"

.PHONY: import
import: ## Import image from tar file (TAR=filename.tar.gz)
ifndef TAR
	@echo "$(RED)❌ TAR variable not set. Use: make import TAR=filename.tar.gz$(RESET)"
	@exit 1
endif
	@echo "$(BLUE)Importing image from $(TAR)...$(RESET)"
	gunzip -c $(TAR) | docker load
	@echo "$(GREEN)✅ Image imported$(RESET)"
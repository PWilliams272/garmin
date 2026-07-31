# Container image for the garmin-data-analyzer Lambda (quality classification
# + GP/STS trend fitting + viewer-cache build). Container image, not a zip,
# because scipy/scikit-learn/statsmodels don't fit a standard Lambda's 250MB
# unzipped code+layers limit.
#
# Uses a plain Debian slim base (not public.ecr.aws/lambda/python) plus the
# Lambda Runtime Interface Client -- AWS's documented "alternative base
# image" pattern. The AWS-provided Lambda Python base image is Amazon Linux
# 2, whose glibc (2.26) is too old for the manylinux_2_28+ wheels current
# numpy/scipy/scikit-learn releases ship -- pip falls back to building from
# source with no compiler installed, and even with one added, compiling
# scipy from source is slow and fragile. Debian's current glibc accepts
# those wheels directly, so every dependency here installs as a prebuilt
# wheel with no compilation at all.
#
# Build from the repo root (paths below assume that build context):
#   docker build -f infra/docker/analyzer.Dockerfile -t garmin-data-analyzer .
FROM python:3.11-slim

# git is needed for the myutils dependency below (installed straight from
# its GitHub repo, not PyPI).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir awslambdaric

ENV LAMBDA_TASK_ROOT=/var/task
WORKDIR ${LAMBDA_TASK_ROOT}

COPY src/garmin/requirements-analyzer.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY src/garmin ${LAMBDA_TASK_ROOT}/garmin

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["garmin.scripts.lambda_analyze.lambda_handler"]

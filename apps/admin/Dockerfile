# TutorTwin control plane - a *separate* Cloud Run service, min instances 0.
#
# Deliberately not bundled into the Python image. Two runtimes in one container
# means one deploy for two things that change at different rates, a base image
# that carries a Node toolchain into the API's attack surface, and a cold start
# that pays for both. They also scale differently: the API is called by a queue
# and a messaging bridge, the admin by a handful of humans.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# The build must not need the API: every page is server-rendered on demand, so
# nothing is fetched at build time. A build that reached the API would make the
# image un-buildable whenever the API was down.
RUN npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=8080

# Never root.
RUN addgroup -g 10001 -S nodejs && adduser -u 10001 -S nextjs -G nodejs

# Next's standalone output ships only the files the server actually imports,
# which is a fraction of node_modules and a much smaller thing to keep patched.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 8080

# No secrets are baked in. TUTORTWIN_API_URL is injected at deploy time, and the
# API session token never exists at build time - it is minted per login and held
# in an httpOnly cookie this server reads.
CMD ["node", "server.js"]

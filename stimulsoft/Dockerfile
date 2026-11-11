FROM node:20-bookworm-slim
WORKDIR /app
COPY package.json ./
RUN npm install
COPY server.js designer.html ./
ENV PORT=8040
EXPOSE 8040
CMD ["node", "server.js"]

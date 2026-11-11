FROM node:20-bookworm-slim
WORKDIR /app
COPY package.json ./
RUN npm install
COPY adapter.js server.js start.js viewer.html designer.html SampleDashboard.mrt ./
ENV PORT=8040
ENV MRT_PATH=/app/SampleDashboard.mrt
EXPOSE 8040 9615
CMD ["node", "start.js"]

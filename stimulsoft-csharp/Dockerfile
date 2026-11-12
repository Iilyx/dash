# Stimulsoft Dashboards.WEB NetCore 2024.3.3 — Linux container
FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm AS build
WORKDIR /src
COPY StimulsoftViewer.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0-bookworm AS final
WORKDIR /app

# libgdiplus + fonts — нужны Stimulsoft на Linux
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgdiplus \
      libc6-dev \
      fontconfig \
      fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

COPY --from=build /app/publish .
RUN mkdir -p /data

ENV ASPNETCORE_URLS=http://0.0.0.0:8050
ENV MRT_PATH=/data/Report.mrt
EXPOSE 8050

ENTRYPOINT ["dotnet", "StimulsoftViewer.dll"]

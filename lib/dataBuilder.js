const { WeatherService } = require('../services/weatherService');
const { AmbientService } = require('../services/ambientService');
const { LLMService } = require('../services/llmService');
const { VehiclesService } = require('../services/vehicleService');
const { CalendarService } = require('../services/calendarService');
const { getStateKey, setStateKey } = require('./state');
const { buildStaticDescription, getWindDirection } = require('./weatherUtils');

/**
 * Build complete dashboard data from all available services
 * Services fail gracefully - only WeatherAPI is required
 * 
 * @param {Object} req - Express request object (for baseUrl)
 * @param {Object} logger - Logger instance
 * @returns {Promise<Object>} Complete dashboard data model
 */
async function buildDashboardData(req, logger = console) {
  const now = new Date();

  // Initialize all services (each service defines its own cache TTL)
  const weatherService = new WeatherService();
  const ambientService = new AmbientService();
  const llmService = new LLMService();
  const vehiclesService = new VehiclesService();
  const calendarService = new CalendarService();

  // Fetch weather data (REQUIRED)
  let weatherData;
  let weatherStatus;
  try {
    const result = await weatherService.getData({}, logger);
    weatherData = result.data;
    weatherStatus = result.status;
  } catch (error) {
    logger.error?.('[DataBuilder] Weather service failed (REQUIRED):', error.message);
    throw new Error(`Weather API unavailable: ${error.message}`);
  }

  // Timezone-aware time formatting
  const formatTime = (date) => {
    if (!weatherData.timezone) return date.toISOString();
    return date.toLocaleString('en-US', { timeZone: weatherData.timezone });
  };

  // Fetch ambient data (OPTIONAL - overrides weather current conditions)
  let ambientData = null;
  let ambientStatus;
  try {
    const result = await ambientService.getData({}, logger);
    ambientData = result.data;
    ambientStatus = result.status;
  } catch (error) {
    logger.info?.('[DataBuilder] Ambient service unavailable (optional):', error.message);
    ambientStatus = ambientService.getStatus();
  }

  // Build current conditions
  // Use ambient sensor data if available, otherwise fall back to Open Weather Map
  // Always use WeatherAPI for condition/icon
  const mainLocation = weatherData.locations[0] || {};
  const current = ambientData ? {
    temp_f: ambientData.current_temp,
    feels_like_f: ambientData.feels_like,
    humidity: ambientData.humidity,
    pressure: ambientData.pressure,
    wind: ambientData.wind,
    weather_icon: mainLocation.icon || 'sunny',
    description: mainLocation.condition || 'Clear',
  } : {
    temp_f: mainLocation.current_temp || 0,
    feels_like_f: mainLocation.current_temp || 0,
    humidity: mainLocation.humidity || 0,
    pressure: mainLocation.pressure || 0,
    wind: {
      speed_mph: mainLocation.wind_mph || 0,
      direction: getWindDirection(mainLocation.wind_dir || 0),
    },
    weather_icon: mainLocation.icon || 'sunny',
    description: mainLocation.condition || 'Clear',
  };

  // Use Ambient precipitation if available, otherwise WeatherAPI
  const precipitation = ambientData?.precipitation || weatherData.precipitation;

  // Fetch vehicles (OPTIONAL)
  let vehicles = [];
  let vehiclesStatus;
  try {
    const result = await vehiclesService.getData({}, logger);
    vehicles = result.data || [];
    vehiclesStatus = result.status;
  } catch (error) {
    logger.info?.('[DataBuilder] Vehicles service unavailable (optional):', error.message);
    vehiclesStatus = vehiclesService.getStatus();
  }

  // Fetch calendar (OPTIONAL)
  let calendar_events = [];
  let calendarStatus;
  try {
    const baseUrl = getBaseUrl(req);
    const calendarConfig = {
      baseUrl,
      timezone: weatherData.timezone,
    };
    const result = await calendarService.getData(calendarConfig, logger);
    calendar_events = result.data || [];
    calendarStatus = result.status;
  } catch (error) {
    logger.info?.('[DataBuilder] Calendar service unavailable (optional):', error.message);
    calendarStatus = calendarService.getStatus();
  }

  // Compute temperature comparison (today's high vs yesterday's high)
  const todayForecast = weatherData.locations[0]?.forecast?.[0];
  const todayHigh = todayForecast?.high_f;
  const tempComparison = computeTempComparison(todayHigh);

  // Build base data model
  const data = {
    current_temp: current.temp_f,
    feels_like: current.feels_like_f,
    weather_icon: current.weather_icon,
    weather_description: current.description,
    date: formatTime(now),
    temp_comparison: tempComparison,
    locations: weatherData.locations,
    forecast: weatherData.forecast,
    hourlyForecast: weatherData.hourlyForecast,
    wind: current.wind,
    sun: weatherData.sun,
    moon: weatherData.moon,
    humidity: current.humidity,
    pressure: current.pressure,
    air_quality: weatherData.air_quality,
    precipitation,
    calendar_events,
    vehicles,
    last_updated: formatTime(now),
  };

  // Fetch LLM insights (OPTIONAL - enriches clothing suggestion and adds daily summary)
  let llmStatus;
  let hasValidInsights = false;
  
  try {
    const llmConfig = {
      input: {
        current,
        forecast: data.forecast,
        hourlyForecast: data.hourlyForecast,
        calendar: data.calendar_events,
        location: data.locations[0],
        timezone: weatherData.timezone,
        sun: data.sun,
        moon: data.moon,
        air_quality: data.air_quality,
      },
    };
    const result = await llmService.getData(llmConfig, logger);
    const insights = result.data;
    llmStatus = result.status;

    // Check if we got valid insights from LLM
    if (insights?.daily_summary && insights.daily_summary.trim().length > 0) {
      data.clothing_suggestion = insights.clothing_suggestion;
      data.daily_summary = insights.daily_summary.trim();
      data.llm_source = result.source;
      hasValidInsights = true;
    }
  } catch (error) {
    logger.info?.('[DataBuilder] LLM service error (optional):', error.message);
    llmStatus = llmService.getStatus();
  }
  
  // If no valid insights (disabled, error, or invalid response), try fallbacks
  if (!hasValidInsights) {
    let usedCache = false;
    
    // Try to use stale cache for LLM
    try {
      const staleCache = llmService.getCache(true); // true = allow stale
      if (staleCache?.daily_summary) {
        data.daily_summary = staleCache.daily_summary.trim();
        if (staleCache.clothing_suggestion) {
          data.clothing_suggestion = staleCache.clothing_suggestion.trim();
        }
        data.llm_source = 'stale_cache';
        usedCache = true;
        logger.info?.('[DataBuilder] Using stale LLM cache');
      }
    } catch (_) {}
    
    // If no cache available, use static description fallback
    if (!usedCache) {
      logger.info?.('[DataBuilder] Using static description fallback');
      const staticDescription = buildStaticDescription({
        current,
        forecast: data.forecast,
        hourlyForecast: data.hourlyForecast,
      });
      data.clothing_suggestion = staticDescription.clothing_suggestion;
      data.daily_summary = staticDescription.daily_summary;
      data.llm_source = 'static_fallback';
    }
  }

  // Attach service statuses for admin panel
  data._serviceStatuses = {
    weather: weatherStatus,
    ambient: ambientStatus,
    llm: llmStatus,
    vehicles: vehiclesStatus,
    calendar: calendarStatus,
  };

  return data;
}

/**
 * Get base URL from request
 * @param {Object} req - Express request object
 * @returns {string} Base URL
 */
function getBaseUrl(req) {
  const proto = (req.headers['x-forwarded-proto'] || req.protocol || 'http').split(',')[0];
  const host = req.get('host');
  return `${proto}://${host}`;
}

/**
 * Get service statuses for admin panel
 * Instantiates services to read their TTL configs, then reads status from state
 * @returns {Object} All service statuses
 */
function getServiceStatuses() {
  const { getStateKey } = require('./state');
  
  // Instantiate services to get their TTL values
  const weatherService = new WeatherService();
  const ambientService = new AmbientService();
  const llmService = new LLMService();
  const vehiclesService = new VehiclesService();
  const calendarService = new CalendarService();
  
  // Service definitions
  const serviceConfigs = {
    weather: { service: weatherService },
    ambient: { service: ambientService },
    llm: { service: llmService },
    vehicles: { service: vehiclesService },
    calendar: { service: calendarService },
  };
  
  const allStatuses = getStateKey('service_status', {});
  const allCaches = getStateKey('service_cache', {});
  
  const statuses = {};
  for (const [key, cfg] of Object.entries(serviceConfigs)) {
    const service = cfg.service;
    const savedStatus = allStatuses[service.cacheKey] || {};
    const cache = allCaches[service.cacheKey];
    const isEnabled = service.isEnabled();
    
    statuses[key] = {
      name: service.name,
      isEnabled,
      state: savedStatus.state || (isEnabled ? 'unknown' : 'disabled'),
      cacheTTL: service.cacheTTL,
      fetchedAt: cache?.fetchedAt || null,
      latency: savedStatus.latency || null,
      error: savedStatus.error || null,
    };
  }
  
  return statuses;
}

/**
 * Compare today's high with yesterday's high
 * Stores daily highs for the last 3 days for historical comparison
 * @param {number} todayHigh - Today's forecast high temperature
 * @returns {string|null} Comparison string or null if no yesterday data
 */
function computeTempComparison(todayHigh) {
  if (todayHigh == null) return null;
  
  // Use local dates, not UTC (important for timezone-aware comparison)
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  
  // Get yesterday's date
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayStr = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, '0')}-${String(yesterday.getDate()).padStart(2, '0')}`;
  
  // Get daily highs history (simple object: { "2025-10-04": 75, "2025-10-03": 68, ... })
  const dailyHighs = getStateKey('daily_highs', {});
  
  // Store today's high if not already stored for today
  if (!dailyHighs[todayStr]) {
    dailyHighs[todayStr] = todayHigh;
    
    // Keep only last 3 days
    const allDates = Object.keys(dailyHighs).sort().reverse();
    const recentDates = allDates.slice(0, 3);
    const trimmedHighs = {};
    recentDates.forEach(date => {
      trimmedHighs[date] = dailyHighs[date];
    });
    
    setStateKey('daily_highs', trimmedHighs);
  }
  
  // Compare with yesterday's high if available
  const yesterdayHigh = dailyHighs[yesterdayStr];
  if (yesterdayHigh == null) return null; // No comparison available
  
  const diff = Number(todayHigh) - Number(yesterdayHigh);
  
  // Determine comparison based on temperature difference (Celsius-appropriate thresholds)
  if (Math.abs(diff) < 0.5) {
    return 'Same as yesterday';
  } else if (diff >= 5) {
    return 'Much warmer than yesterday';
  } else if (diff > 0) {
    return 'Warmer than yesterday';
  } else if (diff <= -5) {
    return 'Much cooler than yesterday';
  } else {
    return 'Cooler than yesterday';
  }
}

module.exports = {
  buildDashboardData,
  getServiceStatuses,
};

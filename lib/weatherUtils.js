/**
 * Weather utility functions shared across weather services
 */

/**
 * Map weather condition text to icon and description
 * This function analyzes weather condition text and returns a standardized
 * icon code and description that can be used across the application.
 * 
 * @param {string} conditionText - Weather condition text from API
 * @returns {Object} Object with icon code and description
 * @returns {string} return.icon - Standardized icon code (e.g., 'sunny', 'rain', 'cloudy')
 * @returns {string} return.description - Human-readable description
 * 
 * @example
 * mapIconAndDescription('Partly Cloudy')
 * // Returns: { icon: 'partly_cloudy', description: 'Partly Cloudy' }
 */
function mapIconAndDescription(conditionText = '') {
  const text = String(conditionText).toLowerCase();
  
  // Thunderstorms and severe weather
  if (/(thunder|storm)/.test(text)) {
    return { icon: 'stormy', description: conditionText || 'Stormy' };
  }
  
  // Snow and winter precipitation
  if (/(snow|sleet|blizzard)/.test(text)) {
    return { icon: 'snow', description: conditionText || 'Snow' };
  }
  
  // Rain and precipitation
  if (/(rain|drizzle|showers)/.test(text)) {
    return { icon: 'rain', description: conditionText || 'Rain' };
  }
  
  // Fog and mist
  if (/(fog|mist|haze|smoke)/.test(text)) {
    return { icon: 'fog', description: conditionText || 'Fog' };
  }
  
  // Partly cloudy (must check before fully cloudy)
  if (/(partly|mostly)\s*(cloudy|sunny)/.test(text)) {
    return { icon: 'partly_cloudy', description: conditionText || 'Partly Cloudy' };
  }
  
  // Cloudy and overcast
  if (/(overcast|cloud)/.test(text)) {
    return { icon: 'cloudy', description: conditionText || 'Cloudy' };
  }
  
  // Clear and sunny (default)
  if (/(clear|sunny|fair)/.test(text)) {
    return { icon: 'sunny', description: conditionText || 'Clear' };
  }
  
  // Default fallback
  return { icon: 'sunny', description: conditionText || 'Clear' };
}

/**
 * Convert wind direction degrees to cardinal direction text
 * 
 * @param {number} degrees - Wind direction in degrees (0-360)
 * @returns {string} Cardinal direction (e.g., 'N', 'NE', 'SSW')
 * 
 * @example
 * getWindDirection(45)
 * // Returns: 'NE'
 */
function getWindDirection(degrees) {
  const directions = [
    'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
  ];
  const index = Math.round(degrees / 22.5) % 16;
  return directions[index];
}

/**
 * Build static weather description as fallback when LLM service is unavailable
 * Generates time-aware descriptions with similar vibe to LLM service
 * 
 * @param {Object} weatherData - Weather data object
 * @param {Object} weatherData.current - Current conditions
 * @param {number} weatherData.current.temp_f - Current temperature
 * @param {string} weatherData.current.description - Current weather description
 * @param {Array} weatherData.forecast - Daily forecast array
 * @param {Array} weatherData.hourlyForecast - Hourly forecast array
 * @returns {Object} Object with clothing_suggestion and daily_summary
 */
function buildStaticDescription(weatherData) {
  const hour = new Date().getHours();
  const timeContext = getTimeContext(hour);
  
  const current = weatherData.current || {};
  const hourlyForecast = weatherData.hourlyForecast || [];
  
  // Forecast array structure: forecast[0] is the next full day
  // During daytime: use forecast[0] for "today's" forecast
  // During nighttime: forecast[0] is actually tomorrow
  const todayForecast = weatherData.forecast?.[0] || {};
  const tomorrowForecast = weatherData.forecast?.[0] || {}; // Tomorrow is also forecast[0] during night
  
  const currentTemp = Number(current.temp_f || 60);
  const highTemp = Number(todayForecast.high_f || todayForecast.high || currentTemp);
  const lowTemp = Number(todayForecast.low_f || todayForecast.low || currentTemp);
  const condition = (current.description || 'Clear').toLowerCase();
  
  // Determine if rain is expected
  const maxRainChance = Math.max(
    todayForecast.rain_chance || 0,
    ...hourlyForecast.slice(0, 6).map(h => h.rain_chance || 0)
  );
  const isRainy = maxRainChance > 40 || /rain|shower|drizzle/.test(condition);
  
  // Get hourly temps to detect swings
  const hourlyTemps = hourlyForecast.slice(0, 8).map(h => h.temp_f);
  const tempRange = hourlyTemps.length > 0 
    ? Math.max(...hourlyTemps) - Math.min(...hourlyTemps)
    : highTemp - lowTemp;
  
  const hasBigSwing = tempRange >= 8;
  
  // Build context based on time of day
  let summary, clothing;
  
  if (timeContext.period === 'morning') {
    ({ summary, clothing } = buildMorningSummary({
      currentTemp, highTemp, lowTemp, condition, isRainy, hasBigSwing, tempRange
    }));
  } else if (timeContext.period === 'afternoon') {
    ({ summary, clothing } = buildAfternoonSummary({
      currentTemp, highTemp, lowTemp, condition, isRainy
    }));
  } else if (timeContext.period === 'evening') {
    ({ summary, clothing } = buildEveningSummary({
      currentTemp, lowTemp, condition, isRainy
    }));
  } else {
    // Night - talk about tomorrow
    const tomorrowHigh = Number(tomorrowForecast.high_f || tomorrowForecast.high || highTemp);
    const tomorrowLow = Number(tomorrowForecast.low_f || tomorrowForecast.low || lowTemp);
    ({ summary, clothing } = buildNightSummary({
      tomorrowHigh, tomorrowLow, condition, isRainy
    }));
  }
  
  return {
    clothing_suggestion: clothing,
    daily_summary: summary,
  };
}

function getTimeContext(hour) {
  if (hour >= 5 && hour < 11) {
    return { period: 'morning' };
  } else if (hour >= 11 && hour < 16) {
    return { period: 'afternoon' };
  } else if (hour >= 16 && hour < 20) {
    return { period: 'evening' };
  } else {
    return { period: 'night' };
  }
}

function buildMorningSummary({ currentTemp, highTemp, lowTemp, condition, isRainy, hasBigSwing, tempRange }) {
  const isCold = currentTemp < 10;
  const isWarm = currentTemp >= 21;
  const willWarmUp = highTemp - currentTemp >= 7;
  
  let summary, clothing;
  
  if (isRainy) {
    clothing = "Rain gear and warm layers";
    if (isCold) {
      summary = "Chilly and rainy morning, staying wet and cool throughout the day";
    } else {
      summary = "Rainy start continuing through the day, stay dry and cozy inside";
    }
  } else if (hasBigSwing && willWarmUp) {
    clothing = "Layers you can shed later";
    if (/fog|mist/.test(condition)) {
      summary = "Cool and foggy this morning, clearing to warmer skies by afternoon";
    } else {
      summary = `Cool start warming up fast, pleasant ${Math.round(tempRange)}° swing by afternoon`;
    }
  } else if (isCold) {
    clothing = "Warm jacket and layers";
    if (/cloud|overcast/.test(condition)) {
      summary = "Chilly and cloudy morning, staying fairly cool throughout the day";
    } else {
      summary = "Crisp cool morning, staying on the cooler side all day long";
    }
  } else if (isWarm) {
    clothing = "Light layers, shorts weather";
    summary = "Warm start to a beautiful day, staying sunny and pleasant throughout";
  } else {
    clothing = "Light jacket for morning";
    if (/cloud/.test(condition)) {
      summary = "Mild and cloudy morning, comfortable temperatures all day long";
    } else {
      summary = "Pleasant morning with comfortable temps, nice conditions all day";
    }
  }
  
  return { summary, clothing };
}

function buildAfternoonSummary({ currentTemp, highTemp, lowTemp, condition, isRainy }) {
  const isHot = currentTemp >= 29;
  const isWarm = currentTemp >= 21;
  const isCool = currentTemp < 16;
  
  let summary, clothing;
  
  if (isRainy) {
    clothing = "Umbrella and light jacket";
    summary = "Rainy afternoon continuing into evening, staying wet and overcast";
  } else if (isHot) {
    clothing = "Light breathable clothes";
    summary = "Hot afternoon continuing, staying warm as we head into the evening";
  } else if (isWarm) {
    clothing = "Light layers for evening";
    if (/clear|sunny/.test(condition)) {
      summary = "Beautiful sunny afternoon, staying pleasant as the day winds down";
    } else {
      summary = "Mild and comfortable afternoon, nice conditions into the evening";
    }
  } else if (isCool) {
    clothing = "Sweater for the rest of day";
    summary = "Cool and comfortable afternoon, staying on the cooler side tonight";
  } else {
    clothing = "Light jacket for evening";
    summary = "Pleasant afternoon temperatures, comfortable conditions into evening";
  }
  
  return { summary, clothing };
}

function buildEveningSummary({ currentTemp, lowTemp, condition, isRainy }) {
  const isCool = currentTemp < 16;
  const willCoolDown = currentTemp - lowTemp >= 6;
  
  let summary, clothing;
  
  if (isRainy) {
    clothing = "Jacket and umbrella";
    summary = "Rainy evening ahead, staying wet and cool as the night sets in";
  } else if (willCoolDown) {
    clothing = "Warm jacket for tonight";
    if (isCool) {
      summary = "Cool evening getting chillier, bundle up as temperatures drop tonight";
    } else {
      summary = "Mild now but cooling down, grab a jacket as the evening progresses";
    }
  } else if (isCool) {
    clothing = "Warm layers for tonight";
    if (/clear/.test(condition)) {
      summary = "Cool and clear evening, staying crisp with nice skies through tonight";
    } else {
      summary = "Cool evening settling in, staying on the chilly side through the night";
    }
  } else {
    clothing = "Light jacket optional";
    if (/clear/.test(condition)) {
      summary = "Pleasant evening with clear skies, comfortable conditions tonight";
    } else {
      summary = "Mild and comfortable evening, nice conditions as the night sets in";
    }
  }
  
  return { summary, clothing };
}

function buildNightSummary({ tomorrowHigh, tomorrowLow, condition, isRainy }) {
  const willBeCold = tomorrowLow < 10;
  const willBeHot = tomorrowHigh >= 29;
  const willBeWarm = tomorrowHigh >= 21;
  
  let summary, clothing;
  
  if (isRainy) {
    clothing = "Rain gear ready for tomorrow";
    if (willBeCold) {
      summary = "Tomorrow rainy and cool, expect wet conditions and chilly temperatures";
    } else {
      summary = "Tomorrow bringing rain and clouds, stay dry with umbrella and layers";
    }
  } else if (willBeHot) {
    clothing = "Light clothes for tomorrow";
    summary = "Tomorrow heating up nicely, expect warm sunny skies and hot temperatures";
  } else if (willBeWarm) {
    clothing = "Comfortable layers for tomorrow";
    if (/fog|mist/.test(condition)) {
      summary = "Tomorrow foggy start clearing out, warming to pleasant afternoon temps";
    } else {
      summary = "Tomorrow pleasant and mild, comfortable temperatures throughout the day";
    }
  } else if (willBeCold) {
    clothing = "Warm layers for tomorrow";
    if (/cloud/.test(condition)) {
      summary = "Tomorrow cool and cloudy, staying on the chilly side all day long";
    } else {
      summary = "Tomorrow crisp and cool, bundle up for chilly temperatures ahead";
    }
  } else {
    clothing = "Light jacket for tomorrow";
    summary = "Tomorrow comfortable and mild, nice conditions throughout the day ahead";
  }
  
  return { summary, clothing };
}

module.exports = {
  mapIconAndDescription,
  getWindDirection,
  buildStaticDescription,
};

const axios = require('axios');
const { BaseService } = require('../lib/BaseService');
const { mapIconAndDescription } = require('../lib/weatherUtils');
const zipcodes = require('zipcodes');

class WeatherService extends BaseService {
  constructor(cacheTTLMinutes = 30) {
    super({
      name: 'OpenWeatherMap Weather',
      cacheKey: 'weather',
      cacheTTL: cacheTTLMinutes * 60 * 1000,
      retryAttempts: 3,
      retryCooldown: 1000,
    });
  }

  isEnabled() {
    return !!process.env.OPENWEATHER_API_KEY;
  }

  async resolveLocation(input) {
    const apiKey = process.env.OPENWEATHER_API_KEY;

    // Numeric ZIP/PIN + country for IN/US
    if (/^\d+,[A-Za-z]{2}$/.test(input)) {
      const [zip, cc] = input.split(',');
      const url = `https://api.openweathermap.org/geo/1.0/zip?zip=${encodeURIComponent(zip)},${cc}&appid=${apiKey}`;
      const resp = await axios.get(url, { timeout: 10000 });
      if (resp.data && resp.data.lat != null && resp.data.lon != null) {
        return {
          latitude: resp.data.lat,
          longitude: resp.data.lon,
          name: resp.data.name || zip,
          country: resp.data.country || cc,
        };
      }
      throw new Error(`ZIP geocoding failed for ${input}`);
    }

    // City name + country
    if (/^[A-Za-z .-]+,[A-Za-z]{2}$/.test(input)) {
      const url = `https://api.openweathermap.org/geo/1.0/direct?q=${encodeURIComponent(input)}&limit=1&appid=${apiKey}`;
      const resp = await axios.get(url, { timeout: 10000 });
      if (resp.data && resp.data[0]) {
        const place = resp.data[0];
        return {
          latitude: place.lat,
          longitude: place.lon,
          name: place.name,
          country: place.country,
        };
      }
      throw new Error(`City geocoding failed for ${input}`);
    }

    // Direct lat/lon
    if (/^-?\d{1,3}\.\d+,-?\d{1,3}\.\d+$/.test(input)) {
      const [lat, lon] = input.split(',').map(Number);
      return {
        latitude: lat,
        longitude: lon,
        name: `${lat},${lon}`,
        country: '',
      };
    }

    throw new Error(`Invalid location format: ${input}`);
  }

  async fetchLocationWeather(loc, units, logger) {
    try {
      const apiKey = process.env.OPENWEATHER_API_KEY;
      const owmUrl = `https://api.openweathermap.org/data/3.0/onecall?lat=${loc.latitude}&lon=${loc.longitude}&units=${units}&exclude=minutely&appid=${apiKey}`;
      const resp = await axios.get(owmUrl, { timeout: 20000 });
      if (resp.status !== 200 || !resp.data) throw new Error(`OneCall returned status ${resp.status}`);
      return { location: loc, data: resp.data };
    } catch (e) {
      logger?.error?.(`Weather fetch error for ${loc.name}: ${e.message}`);
      return null;
    }
  }

  async fetchData(config, logger) {
    const mainLoc = (process.env.MAIN_LOCATION || '').trim();
    const addLocs = (process.env.ADDITIONAL_LOCATIONS || '').split(';').map(l => l.trim()).filter(Boolean);
    const locInputs = mainLoc ? [mainLoc, ...addLocs] : [];
  
    if (locInputs.length === 0) throw new Error('MAIN_LOCATION not configured');
  
    // Use WEATHER_UNITS env, default to 'metric'
    const units = (process.env.WEATHER_UNITS === 'imperial') ? 'imperial' : 'metric';
  
    const resolvedLocs = [];
    for (const input of locInputs) {
      try {
        const locData = await this.resolveLocation(input);
        resolvedLocs.push(locData);
      } catch (e) {
        logger?.error?.(`Weather fetch error for ${input}: ${e.message}`);
      }
    }
    if (resolvedLocs.length === 0) throw new Error('No valid locations found');
  
    const fetchPromises = resolvedLocs.map(loc => this.fetchLocationWeather(loc, units, logger));
    const rawResults = await Promise.all(fetchPromises);
    const results = rawResults.filter(Boolean);
    if (results.length === 0) throw new Error('No weather data could be fetched');
    return results;
  }
  
  mapToDashboard(apiResults, config) {
    if (!Array.isArray(apiResults) || apiResults.length === 0) throw new Error('No weather data available');
    const getWeekday = (dt, tz) =>
      new Date(dt * 1000).toLocaleDateString('en-US', { weekday: 'short', timeZone: tz || 'UTC' });
    const toISODate = dt => new Date(dt * 1000).toISOString().slice(0, 10);
  
    const isMetric = (process.env.WEATHER_UNITS === 'metric');
  
    const convertedWindSpeed = (speed) => {
      if (speed == null) return null;
      // Wind speed from OpenWeatherMap is m/s (metric) or miles/hour (imperial)
      // Convert to km/h when metric units
      return isMetric ? Math.round(speed * 3.6) : Math.round(speed);
    };
  
    const processedLocations = apiResults.map(({ location, data }) => {
      const tz = data.timezone || 'UTC';
      const current = data.current || {};
  
      const forecastDays = (data.daily || []).map(day => ({
        date: toISODate(day.dt),
        day_of_week: getWeekday(day.dt, tz) || 'N/A',
        high: typeof day.temp?.max === 'number' ? Math.round(day.temp.max) : null,
        low: typeof day.temp?.min === 'number' ? Math.round(day.temp.min) : null,
        condition: (day.weather?.[0]?.description || '').toLowerCase(),
        rain_chance: typeof day.pop === 'number' ? Math.round(day.pop * 100) : 0,
        precip_mm: day.rain ? parseFloat(day.rain.toFixed(2)) : 0,
        avghumidity: day.humidity,
        hour: [] // filled later
      }));
  
      if (Array.isArray(data.hourly)) {
        const dayHourBuckets = forecastDays.map(() => []);
        for (const hr of data.hourly) {
          const dateIdx = forecastDays.findIndex(fd => toISODate(hr.dt) === fd.date);
          if (dateIdx >= 0 && dayHourBuckets[dateIdx].length < 24) {
            const hourStr = new Date(hr.dt * 1000).toISOString().substr(11, 8);
            dayHourBuckets[dateIdx].push({
              time: hourStr,
              temp: typeof hr.temp === "number" ? Math.round(hr.temp) : null,
              condition: (hr.weather?.[0]?.description || '').toLowerCase(),
              rain_chance: typeof hr.pop === "number" ? Math.round(hr.pop * 100) : 0,
              wind_speed: convertedWindSpeed(hr.wind_speed),
            });
          }
        }
        forecastDays.forEach((fd, idx) => fd.hour = dayHourBuckets[idx] || []);
      }
  
      const todaySunrise = (data.daily?.[0]?.sunrise ? new Date(data.daily[0].sunrise * 1000) : null);
      const todaySunset = (data.daily?.[0]?.sunset ? new Date(data.daily[0].sunset * 1000) : null);
  
      return {
        location: {
          name: location.name,
          country: location.country,
          zip_code: location.name,
          tz_id: tz
        },
        current: {
          temp: typeof current.temp === "number" ? Math.round(current.temp) : null,
          feels_like: typeof current.feels_like === "number" ? Math.round(current.feels_like) : null,
          humidity: current.humidity,
          pressure: typeof current.pressure === "number" ? Math.round(current.pressure) : null,
          wind_speed: convertedWindSpeed(current.wind_speed),
          wind_dir: current.wind_deg,
          condition: (current.weather?.[0]?.description || '').toLowerCase()
        },
        forecast: forecastDays,
        astro: {
          sunrise: todaySunrise ? this.formatTime12Hour(`${todaySunrise.getHours()}:${todaySunrise.getMinutes()}`) : '',
          sunset: todaySunset ? this.formatTime12Hour(`${todaySunset.getHours()}:${todaySunset.getMinutes()}`) : '',
          moon_phase: typeof data.daily?.[0]?.moon_phase === "number" ? this.convertMoonPhaseString(data.daily[0].moon_phase) : 'New Moon',
          moon_direction: typeof data.daily?.[0]?.moon_phase === "number" ? this.convertMoonPhaseDirection(data.daily[0].moon_phase) : 'waxing',
          moon_illumination: typeof data.daily?.[0]?.moon_phase === "number" ? this.calculateMoonIllumination(data.daily[0].moon_phase) : null,
        }
      };
    });

    // Locations for dashboard (today)
    const locations = processedLocations.map(loc => {
      const today = loc.forecast[0] || {};
      const icon = mapIconAndDescription(loc.current.condition || '').icon;
      return {
        name: loc.location.name,
        country: loc.location.country,
        zip_code: loc.location.zip_code,
        current_temp: Math.round(loc.current.temp || 0),
        high: Math.round(today.high || 0),
        low: Math.round(today.low || 0),
        icon,
        condition: loc.current.condition || 'Clear',
        rain_chance: Number(today.rain_chance || 0),
        humidity: Math.round(loc.current.humidity || 0),
        pressure: Math.round(Number(loc.current.pressure || 0)),
        wind_speed: Math.round(Number(loc.current.wind_speed || 0)),
        wind_dir: loc.current.wind_dir || 0
      };
    });

    // 5-day forecast skip today
    const mainLocation = processedLocations[0];
    const allForecast = mainLocation.forecast.map(day => {
      const { icon } = mapIconAndDescription(day.condition || '');
      return {
        date: day.date,
        day: day.day_of_week || 'N/A',
        high: Math.round(day.high || 0),
        low: Math.round(day.low || 0),
        icon,
        rain_chance: Number(day.rain_chance || 0),
      };
    });
    const now = new Date();
    const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
    const todayIndex = allForecast.findIndex(d => d.date === todayStr);
    const startIndex = todayIndex >= 0 ? todayIndex + 1 : 1;
    const forecast = allForecast.slice(startIndex, startIndex + 5);

    // Hourly forecast (metric)
    const hourlyForecast = [];
    if (mainLocation.forecast[0] && Array.isArray(mainLocation.forecast[0].hour)) {
      const hours = mainLocation.forecast[0].hour;
      const nowHour = now.getHours();
      for (let i = 0; i < hours.length && hourlyForecast.length < 24; i++) {
        const [hStr] = (hours[i].time || '00:00:00').split(':');
        const hourInt = parseInt(hStr, 10);
        if (i === 0 && hourInt < nowHour) continue;
        const { icon } = mapIconAndDescription(hours[i].condition || '');
        const hourNum = hourInt % 12 || 12;
        const ampm = hourInt < 12 ? 'AM' : 'PM';
        hourlyForecast.push({
          time: `${hourNum} ${ampm}`,
          temp: Math.round(Number(hours[i].temp || 0)),
          condition: hours[i].condition || 'Unknown',
          icon,
          rain_chance: Number(hours[i].rain_chance || 0),
          wind_speed: Math.round(Number(hours[i].wind_speed || 0)),
        });
      }
    }

    // Precip totals (mm)
    const total24h = mainLocation.forecast[0]?.precip_mm || 0;
    const weekTotal = mainLocation.forecast.slice(0, 7).reduce(
      (sum, d) => sum + Number(d.precip_mm || 0), 0
    );

    return {
      locations,
      forecast,
      hourlyForecast,
      timezone: mainLocation.location.tz_id,
      sun: {
        sunrise: mainLocation.astro.sunrise,
        sunset: mainLocation.astro.sunset,
      },
      moon: {
        phase: mainLocation.astro.moon_phase,
        direction: mainLocation.astro.moon_direction,
        illumination: mainLocation.astro.moon_illumination ? Number(mainLocation.astro.moon_illumination) : null,
      },
      air_quality: { aqi: null, category: 'Unknown' },
      precipitation: {
        last_24h_mm: Number(total24h.toFixed(2)),
        week_total_mm: Number(weekTotal.toFixed(2)),
        year_total_mm: null,
      },
    };
  }

  formatTime12Hour(timeStr) {
    if (!timeStr) return '';
    const [hStr, mStr] = timeStr.split(':');
    const hours24 = parseInt(hStr, 10);
    const minutes = mStr.padStart(2, '0');
    const period = hours24 >= 12 ? 'PM' : 'AM';
    const hours12 = hours24 % 12 || 12;
    return `${hours12}:${minutes} ${period}`;
  }

  convertMoonPhaseString(moonphase) {
    if (moonphase == null) return 'New Moon';
    const phase = Number(moonphase);
    if (phase === 0) return 'New Moon';
    if (phase < 0.25) return 'Waxing Crescent';
    if (phase === 0.25) return 'First Quarter';
    if (phase < 0.5) return 'Waxing Gibbous';
    if (phase === 0.5) return 'Full Moon';
    if (phase < 0.75) return 'Waning Gibbous';
    if (phase === 0.75) return 'Last Quarter';
    if (phase < 1) return 'Waning Crescent';
    return 'New Moon';
  }

  convertMoonPhaseDirection(moonphase) {
    if (moonphase == null) return 'waxing';
    const phase = Number(moonphase);
    if (phase <= 0.5) return 'waxing';
    return 'waning';
  }

  calculateMoonIllumination(moonphase) {
    if (moonphase == null) return 0;
    const phase = Number(moonphase);
    if (phase <= 0.5) return Math.round(phase * 2 * 100);
    return Math.round((1 - phase) * 2 * 100);
  }
}

module.exports = { WeatherService };

# get_available_documents.py
from flask import current_app
from datetime import datetime
from renglo.data.data_controller import DataController
from renglo.docs.docs_controller import DocsController
from renglo.auth.auth_controller import AuthController
from renglo.chat.chat_controller import ChatController
from renglo.blueprint.blueprint_controller import BlueprintController
from renglo.common import load_config

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Any, Union, List, Optional
from decimal import Decimal
from openai import OpenAI


import json
import re

# Custom JSON encoder to handle Decimal objects
class DecimalEncoder(json.JSONEncoder):
    """
    Custom JSON encoder to handle Decimal objects.

    Converts Decimal objects to float for JSON serialization, as the
    standard JSON encoder does not support Decimal types.

    Examples
    --------
    >>> encoder = DecimalEncoder()
    >>> json.dumps({'price': Decimal('100.50')}, cls=DecimalEncoder)
    '{"price": 100.5}'
    """
    def default(self, obj):
        """
        Convert Decimal to float, otherwise use default encoding.

        Parameters
        ----------
        obj : any
            Object to encode

        Returns
        -------
        float or any
            Float if obj is Decimal, otherwise default encoding
        """
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

@dataclass
class RequestContext:
    """
    Request context for maintaining state during handler execution.

    This dataclass stores context information that persists across method
    calls within a single handler execution, including portfolio/org identifiers,
    entity information, thread and leg identifiers, and cached search results.

    Attributes
    ----------
    portfolio : str
        Portfolio ID
    org : str
        Organization ID
    entity_type : str
        Entity type (e.g., 'noma_travels')
    entity_id : str
        Entity ID (e.g., trip_id in format 'org-trip-<trip_id>')
    thread : str
        Thread ID
    leg : str
        Leg ID (e.g., '0', '1', 'return')
    search_results : Dict[str, Any]
        Cached search results from API calls
    query_params : Dict[str, Any]
        Query parameters used in operations
    """
    portfolio: str = ''
    org: str = ''
    entity_type: str = ''
    entity_id: str = ''
    thread: str = ''
    leg: str = ''
    search_results: Dict[str, Any] = field(default_factory=dict)
    query_params: Dict[str, Any] = field(default_factory=dict)

# Create a context variable to store the request context
request_context: ContextVar[RequestContext] = ContextVar('request_context', default=RequestContext())

class AddBundle:
    """
    Add a bundle to the trip document.

    """

    def __init__(self):
        """
        Initialize AddBundle handler.

        Notes
        -----
        Loads configuration and initializes:
        - OpenAI client for LLM interactions (if API key available)
        - All required controllers (DataController, AuthController, etc.)
        - LLM model names (gpt-3.5-turbo for primary, gpt-4o-mini for secondary)

        Raises
        ------
        Prints error message if OpenAI client initialization fails, but continues
        with None value (handler will fail later if LLM is needed)
        """
        # Load config for handlers (independent of Flask)
        config = load_config()

        #OpenAI Client
        try:
            openai_api_key = config.get('OPENAI_API_KEY', '')
            openai_client = OpenAI(api_key=openai_api_key)
            print(f"OpenAI client initialized")
        except Exception as e:
            print(f"Error initializing OpenAI client: {e}")
            openai_client = None

        self.AI_1 = openai_client
        #self.AI_1_MODEL = "gpt-4" // This model does not support json_object response format
        self.AI_1_MODEL = "gpt-3.5-turbo" # Baseline model. Good for multi-step chats
        self.AI_2_MODEL = "gpt-4o-mini" # This model is not very smart


        self.DAC = DataController(config=config)
        self.AUC = AuthController(config=config)
        self.DCC = DocsController(config=config)
        self.BPC = BlueprintController(config=config)
        self.CHC = ChatController(config=config)



    def _get_context(self) -> RequestContext:
        """
        Get the current request context.

        Returns
        -------
        RequestContext
            Current request context instance from context variable
        """
        return request_context.get()

    def _set_context(self, context: RequestContext):
        """
        Set the current request context.

        Parameters
        ----------
        context : RequestContext
            Request context instance to set
        """
        request_context.set(context)

    def _update_context(self, **kwargs):
        """
        Update specific fields in the current request context.

        Parameters
        ----------
        **kwargs
            Keyword arguments matching RequestContext field names
            (portfolio, org, entity_type, entity_id, thread, leg, etc.)

        Notes
        -----
        Updates only the specified fields, leaving others unchanged.
        """
        context = self._get_context()
        for key, value in kwargs.items():
            setattr(context, key, value)
        self._set_context(context)

    def sanitize(self, obj: Any) -> Any:
        """
        Recursively convert Decimal objects to regular numbers in nested data structures.

        Traverses nested dictionaries and lists, converting all Decimal
        objects to int (if whole number) or float (if decimal).

        Parameters
        ----------
        obj : any
            Object to sanitize (dict, list, Decimal, or other)

        Returns
        -------
        any
            Same structure with Decimal objects converted to int/float

        Examples
        --------
        >>> handler.sanitize({'price': Decimal('100.50'), 'count': Decimal('5')})
        {'price': 100.5, 'count': 5}
        """
        if isinstance(obj, list):
            return [self.sanitize(x) for x in obj]
        elif isinstance(obj, dict):
            return {k: self.sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, Decimal):
            # Convert Decimal to int if it's a whole number, otherwise float
            return int(obj) if obj % 1 == 0 else float(obj)
        else:
            return obj

    def _validate_airport(self, airport: Dict[str, Any], airport_name: str) -> tuple[bool, str]:
        """
        Validate airport structure.

        Checks that an airport object has the required fields and correct types.

        Parameters
        ----------
        airport : dict
            Airport object to validate
        airport_name : str
            Name of airport field (for error messages, e.g., 'departure_airport')

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, ""). If invalid, returns (False, error_message)

        Notes
        -----
        Required fields:
        - 'id': str (IATA code, 3 letters)
        - 'name': str (airport name)
        - 'time': str (ISO 8601 format timestamp)
        """
        if not isinstance(airport, dict):
            return False, f"{airport_name} must be a dictionary"

        required_fields = ["id", "name", "time"]
        for field in required_fields:
            if field not in airport:
                return False, f"{airport_name} missing required field: {field}"
            if not isinstance(airport[field], str):
                return False, f"{airport_name}.{field} must be a string"

        return True, ""

    def _validate_flight(self, flight: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate individual flight structure.

        Checks that a flight object has all required fields and correct types,
        including validation of nested airport objects.

        Parameters
        ----------
        flight : dict
            Flight object to validate

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, ""). If invalid, returns (False, error_message)

        Notes
        -----
        Required fields:
        - 'airline': str
        - 'airline_logo': str
        - 'airplane': str
        - 'arrival_airport': dict (validated via _validate_airport)
        - 'departure_airport': dict (validated via _validate_airport)
        - 'duration': str
        - 'extensions': list of str
        - 'flight_number': str
        - 'legroom': str
        - 'travel_class': str
        """
        if not isinstance(flight, dict):
            return False, "Flight must be a dictionary"

        required_fields = [
            "airline", "airline_logo", "airplane", "arrival_airport",
            "departure_airport", "duration", "extensions", "flight_number",
            "legroom", "travel_class"
        ]

        for field in required_fields:
            if field not in flight:
                return False, f"Flight missing required field: {field}"

        # Validate string fields
        string_fields = ["airline", "airline_logo", "airplane",
                       "flight_number", "travel_class"]
        for field in string_fields:
            if not isinstance(flight[field], str):
                return False, f"Flight.{field} must be a string"

        # Validate extensions array
        if not isinstance(flight["extensions"], list):
            return False, "Flight.extensions must be an array"
        for ext in flight["extensions"]:
            if not isinstance(ext, str):
                return False, "Flight.extensions must contain only strings"

        # Validate airports
        valid, error = self._validate_airport(flight["arrival_airport"], "arrival_airport")
        if not valid:
            return False, error

        valid, error = self._validate_airport(flight["departure_airport"], "departure_airport")
        if not valid:
            return False, error

        return True, ""

    def _validate_traveler(self, traveler: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate traveler structure.

        Checks that a traveler object has all required fields and correct types.

        Parameters
        ----------
        traveler : dict
            Traveler object to validate

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, ""). If invalid, returns (False, error_message)

        Notes
        -----
        Required fields:
        - 'available': bool
        - 'avatar': str
        - 'email': str
        - 'id': str
        - 'name': str
        """
        if not isinstance(traveler, dict):
            return False, "Traveler must be a dictionary"

        required_fields = ["available", "avatar", "email", "id", "name"]
        for field in required_fields:
            if field not in traveler:
                return False, f"Traveler missing required field: {field}"

        # Validate types
        if not isinstance(traveler["available"], bool):
            return False, "Traveler.available must be a boolean"

        string_fields = ["avatar", "email", "id", "name"]
        for field in string_fields:
            if not isinstance(traveler[field], str):
                return False, f"Traveler.{field} must be a string"

        return True, ""

    def _validate_carbon_emissions(self, carbon_emissions: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate carbon emissions structure.

        Checks that a carbon_emissions object has required fields.

        Parameters
        ----------
        carbon_emissions : dict
            Carbon emissions object to validate

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, ""). If invalid, returns (False, error_message)

        Notes
        -----
        Required fields:
        - 'this_flight': str (e.g., "250 kg CO2")
        - 'typical_for_this_route': str (e.g., "238 kg CO2")
        """
        if not isinstance(carbon_emissions, dict):
            return False, "carbon_emissions must be a dictionary"

        required_fields = ["this_flight", "typical_for_this_route"]
        for field in required_fields:
            if field not in carbon_emissions:
                return False, f"carbon_emissions missing required field: {field}"
            if not isinstance(carbon_emissions[field], str):
                return False, f"carbon_emissions.{field} must be a string"

        return True, ""

    def _clean_json_string(self, json_str: str) -> str:
        """
        Clean common JSON formatting issues.

        Fixes common problems in JSON strings that prevent parsing:
        - Trailing commas before closing braces/brackets
        - Python boolean values (True/False) to JSON (true/false)
        - Python None to JSON null

        Parameters
        ----------
        json_str : str
            JSON string to clean

        Returns
        -------
        str
            Cleaned JSON string

        Examples
        --------
        >>> handler._clean_json_string('{"key": True,}')
        '{"key": true}'
        """
        import re

        # Remove trailing commas before closing braces and brackets
        # This regex finds commas followed by closing braces/brackets and removes the comma
        json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)

        # Fix Python boolean values to JSON boolean values
        json_str = re.sub(r'\bTrue\b', 'true', json_str)
        json_str = re.sub(r'\bFalse\b', 'false', json_str)

        # Fix None to null
        json_str = re.sub(r'\bNone\b', 'null', json_str)

        return json_str

    def validate_flight_segment(self, payload: Union[Dict[str, Any], str]) -> Dict[str, Any]:
        """
        Validate complete flight segment structure.

        Validates a flight segment (single or array) against the required schema.
        Accepts both dictionary and JSON string formats. Returns validation result
        with parsed and validated data if successful.

        Parameters
        ----------
        payload : dict or str
            Flight segment(s) to validate:
            - dict: Single segment or list of segments
            - str: JSON string containing segment(s)

        Returns
        -------
        dict
            {
                'success': bool,
                'message': str,               # Error message if validation failed
                'output': dict or None        # Validated segment(s) if success, None if failed
            }

        Notes
        -----
        Required fields in flight segment:
        - 'airline_logo': str
        - 'carbon_emissions': dict
        - 'flights': list (non-empty, each flight validated via _validate_flight)
        - 'price': str
        - 'total_duration': str
        - 'type': str

        Examples
        --------
        >>> result = handler.validate_flight_segment({
        ...     'airline_logo': 'https://example.com/logo.png',
        ...     'flights': [...],
        ...     'price': '$500',
        ...     'total_duration': '8h 30m',
        ...     'type': 'direct'
        ... })
        >>> if result['success']:
        ...     segment = result['output']
        """
        import json
        print(f"Segment to be validated: {payload}")


        # Check if payload is a JSON string and parse it
        if isinstance(payload, str):
            try:
                print(f"Attempting to parse JSON string: {payload[:200]}...")  # Show first 200 chars

                # Clean the JSON string first
                cleaned_json = self._clean_json_string(payload)
                print(f"Cleaned JSON string: {cleaned_json[:200]}...")

                parsed_data = json.loads(cleaned_json)
                print(f"JSON parsed successfully, type: {type(parsed_data).__name__}")
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {str(e)}")
                print(f"Error position: line {e.lineno}, column {e.colno}")
                print(f"Error message: {e.msg}")
                return {
                    'success': False,
                    'message': f"Invalid JSON string: {str(e)} at line {e.lineno}, column {e.colno}",
                    'output': None
                }
        elif isinstance(payload, dict):
            parsed_data = payload
        else:
            return {
                'success': False,
                'message': f"Payload must be a dictionary or JSON string, got {type(payload).__name__}",
                'output': None
            }

        print(f"TYPE:{type(parsed_data).__name__}")
        print(parsed_data)
        # Check if it's an array of segments or a single segment
        if isinstance(parsed_data, list):
            valid, message = self._validate_segments_array(parsed_data)
            print('Flag(VFS1):',valid,message)
            return {
                'success': valid,
                'message': message,
                'output': parsed_data if valid else None
            }
        elif isinstance(parsed_data, dict):
            valid, message = self._validate_single_segment(parsed_data)
            print('Flag(VFS2):',valid,message)
            return {
                'success': valid,
                'message': message,
                'output': parsed_data if valid else None
            }
        else:
            return {
                'success': False,
                'message': f"Parsed data must be a dictionary or array, got {type(parsed_data).__name__}",
                'output': None
            }

    def _validate_single_segment(self, segment: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate a single flight segment.

        Checks that a flight segment has all required fields and that the
        flights array contains valid flight objects.

        Parameters
        ----------
        segment : dict
            Single flight segment to validate

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, "segment structure is valid").
            If invalid, returns (False, error_message).

        Notes
        -----
        Validates:
        - Required top-level fields (airline_logo, carbon_emissions, flights, price, total_duration, type)
        - flights array is non-empty
        - Each flight in flights array is valid (via _validate_flight)
        """
        # Main validation logic
        if not isinstance(segment, dict):
            return False, "segment must be a dictionary"

        # Check required top-level fields
        required_fields = [
            "airline_logo", "carbon_emissions",
            "flights", "price", "total_duration", "type"
        ]

        for field in required_fields:
            if field not in segment:
                return False, f"Missing required field: {field}"

        # Validate string fields
        string_fields = ["airline_logo", "type"]
        for field in string_fields:
            if not isinstance(segment[field], str):
                return False, f"{field} must be a string"

        # Validate carbon_emissions
        #valid, error = self._validate_carbon_emissions(segment["carbon_emissions"])
        #if not valid:
        #    return False, error

        # Validate flights array
        if not isinstance(segment["flights"], list):
            return False, "flights must be an array"
        if len(segment["flights"]) == 0:
            return False, "flights array cannot be empty"

        for i, flight in enumerate(segment["flights"]):
            valid, error = self._validate_flight(flight)
            if not valid:
                return False, f"Flight {i}: {error}"

        # Validate travelers array
        '''if not isinstance(segment["travelers"], list):
            return False, "travelers must be an array"
        if len(segment["travelers"]) == 0:
            return False, "travelers array cannot be empty"

        for i, traveler in enumerate(segment["travelers"]):
            valid, error = self._validate_traveler(traveler)
            if not valid:
                return False, f"Traveler {i}: {error}"
        '''

        return True, "segment structure is valid"

    def _validate_segments_array(self, segments: List[Dict[str, Any]]) -> tuple[bool, str]:
        """
        Validate an array of flight segments.

        Validates that all segments in the array are valid flight segments.

        Parameters
        ----------
        segments : list of dict
            Array of flight segments to validate

        Returns
        -------
        tuple
            (is_valid: bool, error_message: str)
            If valid, returns (True, "All N segments are valid").
            If invalid, returns (False, "Segment i: error_message").

        Notes
        -----
        - Array must be non-empty
        - Each segment is validated via _validate_single_segment
        """
        if not isinstance(segments, list):
            return False, "segments must be an array"

        if len(segments) == 0:
            return False, "segments array cannot be empty"

        for i, segment in enumerate(segments):
            print(f'Validating single segment:{segment}')
            valid, error = self._validate_single_segment(segment)
            if not valid:
                return False, f"Segment {i}: {error}"

        return True, f"All {len(segments)} segments are valid"


    def clean_json_response(self, response: str) -> Dict[str, Any]:
        """
        Clean and validate a JSON response string from LLM.

        Cleans common formatting issues in LLM JSON responses and parses
        the result. Handles unquoted property names, single quotes, trailing
        commas, Python booleans, and other common issues.

        Parameters
        ----------
        response : str
            Raw JSON response string from LLM

        Returns
        -------
        dict
            Parsed JSON object

        Raises
        ------
        json.JSONDecodeError
            If the response cannot be parsed as JSON after cleaning

        Notes
        -----
        Cleaning steps:
        1. Remove comments (single-line and multi-line)
        2. Fix unquoted property names
        3. Replace single quotes with double quotes
        4. Fix Python booleans (True/False -> true/false)
        5. Remove trailing commas
        6. Remove timestamps in square brackets
        """
        try:
            # Clean the response by ensuring property names are properly quoted
            #cleaned_response = response.strip()
            cleaned_response = response
            # Remove any comments (both single-line and multi-line)
            cleaned_response = re.sub(r'//.*?$', '', cleaned_response, flags=re.MULTILINE)  # Remove single-line comments
            cleaned_response = re.sub(r'/\*.*?\*/', '', cleaned_response, flags=re.DOTALL)  # Remove multi-line comments

            # First try to parse as is
            try:
                return json.loads(cleaned_response)
            except json.JSONDecodeError:
                pass

            # If that fails, try to fix common issues
            # Handle unquoted property names at the start of the object
            cleaned_response = re.sub(r'^\s*{\s*(\w+)(\s*:)', r'{"\1"\2', cleaned_response)

            # Handle unquoted property names after commas
            cleaned_response = re.sub(r',\s*(\w+)(\s*:)', r',"\1"\2', cleaned_response)

            # Handle unquoted property names after newlines
            cleaned_response = re.sub(r'\n\s*(\w+)(\s*:)', r'\n"\1"\2', cleaned_response)

            # Replace single quotes with double quotes for property names
            cleaned_response = re.sub(r'([{,]\s*)\'(\w+)\'(\s*:)', r'\1"\2"\3', cleaned_response)

            # Replace single quotes with double quotes for string values
            # This regex looks for : 'value' pattern and replaces it with : "value"
            cleaned_response = re.sub(r':\s*\'([^\']*)\'', r': "\1"', cleaned_response)

            # Remove spaces between colons and boolean values
            cleaned_response = re.sub(r':\s+(true|false|True|False)', r':\1', cleaned_response)

            # Remove trailing commas in objects and arrays
            # This regex will match a comma followed by whitespace and then a closing brace or bracket
            cleaned_response = re.sub(r',(\s*[}\]])', r'\1', cleaned_response)

            # Remove any timestamps in square brackets
            cleaned_response = re.sub(r'\[\d+\]\s*', '', cleaned_response)

            # Try to parse the cleaned response
            try:
                return json.loads(cleaned_response)
            except json.JSONDecodeError as e:
                print(f"First attempt failed. Error: {e}")
                #print(f"Cleaned response type: {type(cleaned_response)}")
                #print(f"Cleaned response length: {len(cleaned_response)}")
                #print(f"Cleaned response content: '{cleaned_response}'")

                # If first attempt fails, try to fix the raw field specifically
                # Find the raw field and ensure it's properly formatted
                raw_match = re.search(r'"raw":\s*({[^}]+})', cleaned_response)
                if raw_match:
                    raw_content = raw_match.group(1)
                    # Convert single quotes to double quotes in the raw content
                    raw_content = raw_content.replace("'", '"')
                    # Replace the raw field with the cleaned version
                    cleaned_response = cleaned_response[:raw_match.start(1)] + raw_content + cleaned_response[raw_match.end(1):]

                #print(f"After raw field cleanup - content: '{cleaned_response}'")
                return json.loads(cleaned_response)


        except json.JSONDecodeError as e:
            print(f"Error parsing cleaned JSON response: {e}")
            #print(f"Original response: {response}")
            #print(f"Cleaned response: {cleaned_response}")
            raise



    def llm(self, prompt: Dict[str, Any]) -> Any:
        """
        Call OpenAI API for LLM completion.

        Creates a chat completion request with the provided prompt parameters.
        Handles optional parameters like tools and tool_choice.

        Parameters
        ----------
        prompt : dict
            Prompt parameters containing:
            {
                'model': str,                # Model name (e.g., 'gpt-3.5-turbo')
                'messages': list,            # List of message dicts
                'temperature': float,         # Temperature for generation
                'tools': list, optional       # Tools available to LLM
                'tool_choice': str, optional  # Tool choice strategy
            }

        Returns
        -------
        any
            Response message object from OpenAI API, or False if error

        Raises
        ------
        Exception
            If OpenAI API call fails (returns False instead of raising)

        Notes
        -----
        - Uses self.AI_1 (OpenAI client) initialized in __init__
        - Returns response.choices[0].message on success
        - Returns False on error (prints error message)
        - Note: Decimal objects in values will cause serialization errors
        """
        try:

            # Create base parameters
            params = {
                'model': prompt['model'],
                'messages': prompt['messages'],
                'temperature': prompt['temperature']
            }

            # Add optional parameters if they exist
            if 'tools' in prompt:
                params['tools'] = prompt['tools']
            if 'tool_choice' in prompt:
                params['tool_choice'] = prompt['tool_choice']

            response = self.AI_1.chat.completions.create(**params)

            # chat.completions.create might return an error if you include Decimal() as values
            # Object of type Decimal is not JSON serializable

            return response.choices[0].message


        except Exception as e:
            print(f"Error running LLM call: {e}")
            # Only print raw response if it exists
            return False

    def find_in_cache(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Find Bundle in workspace cache and extract using LLM.
        """
        action = 'find_in_cache'

        try:
            portfolio = self._get_context().portfolio
            org = self._get_context().org
            ring = 'noma_travels'

            entity_type = self._get_context().entity_type
            entity_id = self._get_context().entity_id
            thread = self._get_context().thread

            # Validate payload and extract hint
            if 'hint' not in payload or not payload['hint']:
                hint = 'Choose the first flight'
            else:
                 hint = payload['hint']

            if entity_type == 'org-trip':

                #parts = entity_id.split('-')
                #thread = '-'.join(parts[1:])


                # Get the workspaces in this thread
                response = self.CHC.list_workspaces(portfolio,org,entity_type,entity_id,thread)
                workspaces_list = response['items']
                print('WORKSPACES_LIST >>',workspaces_list)

                if not workspaces_list or len(workspaces_list) == 0:
                    print('No workspaces found')
                    return {
                        'success': False,
                        'action': action,
                        'error': 'No workspaces found for this thread',
                        'output': 0
                    }

                # Extract cache from workspace
                workspace = workspaces_list[-1]
                if 'cache' not in workspace:
                    print('No cache found in workspace')
                    return {
                        'success': False,
                        'action': action,
                        'error': 'No cache found in workspace',
                        'output': 0
                    }

                cache_key = 'irn:tool_rs:inca/generate_bundles'
                if cache_key not in workspace['cache']:
                    print(f'Cache key {cache_key} not found')
                    return {
                        'success': False,
                        'action': action,
                        'error': f'Cache key {cache_key} not found in workspace',
                        'output': 0
                    }

                cache = workspace['cache'][cache_key]['output']

                print('Cache:',cache)

                #Serialize cache
                serialized_cache = json.dumps(cache, indent=2, cls=DecimalEncoder) if cache else "[]"

                if not cache or not isinstance(cache, list):
                    print('Cache is empty or not a list')
                    return {
                        'success': False,
                        'action': action,
                        'error': 'Cache is empty or not a list',
                        'output': 0
                    }

                # Define the JSON schema for bundle objects (matches generate_bundles output)
                json_schema = '''{
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "bundle_id": { "type": "string" },
                        "estimated_total": {
                            "type": "object",
                            "properties": {
                                "amount": { "type": "string" },
                                "currency": { "type": "string" }
                            }
                        },
                        "flight_option_id": { "type": "string" },
                        "flight_option_ids": {
                            "type": "array",
                            "items": { "type": "string" }
                        },
                        "hotel_option_id": { "type": "string" },
                        "hotel_option_ids": {
                            "type": "array",
                            "items": { "type": "string" }
                        },
                        "price_breakdown": {
                            "type": "object",
                            "properties": {
                                "flight_price_per_ticket": {
                                    "type": "object",
                                    "properties": {
                                        "amount": { "type": "string" },
                                        "currency": { "type": "string" }
                                    }
                                },
                                "flight_segments": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "date": { "type": "string" },
                                            "from": { "type": "string" },
                                            "to": { "type": "string" },
                                            "total": {
                                                "type": "object",
                                                "properties": {
                                                    "amount": { "type": "string" },
                                                    "currency": { "type": "string" }
                                                }
                                            }
                                        }
                                    }
                                },
                                "flight_total": {
                                    "type": "object",
                                    "properties": {
                                        "amount": { "type": "string" },
                                        "currency": { "type": "string" }
                                    }
                                },
                                "hotel_stays": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "check_in": { "type": "string" },
                                            "check_out": { "type": "string" },
                                            "guest_count": { "type": "string" },
                                            "location_code": { "type": "string" },
                                            "nights": { "type": "string" },
                                            "price_per_night": {
                                                "type": "object",
                                                "properties": {
                                                    "amount": { "type": "string" },
                                                    "currency": { "type": "string" }
                                                }
                                            },
                                            "total": {
                                                "type": "object",
                                                "properties": {
                                                    "amount": { "type": "string" },
                                                    "currency": { "type": "string" }
                                                }
                                            }
                                        }
                                    }
                                },
                                "hotel_total": {
                                    "type": "object",
                                    "properties": {
                                        "amount": { "type": "string" },
                                        "currency": { "type": "string" }
                                    }
                                },
                                "passenger_count": { "type": ["string", "integer"] },
                                "total": {
                                    "type": "object",
                                    "properties": {
                                        "amount": { "type": "string" },
                                        "currency": { "type": "string" }
                                    }
                                }
                            }
                        },
                        "tradeoffs": {
                            "type": "array",
                            "items": { "type": "string" }
                        },
                        "why_this_bundle": { "type": "string" }
                    },
                    "additionalProperties": true
                }'''

                # Create prompt and call LLM
                prompt_text = f"""
                - You are a very smart assistant that helps the user find a bundle object inside of a cache.
                - The cache is an array of bundle objects (flight + hotel combinations).
                - Given this hint: {hint}, you need to infer which bundle the user is referring to.

                This is the cache (array of bundles):
                {serialized_cache}

                ## Example 1: If the hint is similar to 'I want the first one' or 'the first bundle', the right option is index 0.
                ## Example 2: If the hint is similar to 'The cheapest' or 'lowest price', look at estimated_total.amount or price_breakdown.total.amount in each bundle.
                ## Example 3: If the hint is similar to 'The one that goes to Seattle', look at price_breakdown.flight_segments[].to or price_breakdown.hotel_stays[].location_code.
                ## Example 4: If the hint mentions a specific date, look at price_breakdown.flight_segments[].date or hotel_stays[].check_in/check_out.
                ## Example 5: If the hint mentions a reason, match against the why_this_bundle text in each bundle.
                ## Example 6: If the hint is similar to 'The one with 4 guests', look at price_breakdown.passenger_count or hotel_stays[].guest_count.

                - The JSON Schema for each Bundle object in the array is shown below:

                {json_schema}

                All you need to output is the index number in the array that indicates which bundle the hint refers to.
                If there is no match, return the number 999.

                Return a JSON object with the following structure:
                {{
                    "selection": string
                }}

                """

                prompt = {
                    "model": self.AI_1_MODEL,
                    "messages": [{ "role": "user", "content": prompt_text}],
                    "temperature":0
                }

                #print('add_flight > RAW PROMPT >>',prompt)
                response = self.llm(prompt)
                print('add_flight > RAW RESPONSE >>',response)

                if not response.content:
                    raise Exception('LLM response is empty')

                result = self.clean_json_response(response.content)
                sanitized_result = self.sanitize(result)

                # Parse LLM response
                if 'selection' in sanitized_result:
                    if sanitized_result['selection'] == '999':
                        selected_index = 0
                    else:
                        selected_index = int(sanitized_result['selection'])
                else:
                    selected_index = 0

                # Sanitize the selected flight data to convert Decimal objects to regular numbers
                selected_bundle = self.sanitize(cache[selected_index])
                return {'success':True,'action':action,'input': payload,'output':selected_bundle}

            else:
                return {
                    'success': False,
                    'action': action,
                    'error': f'Unsupported entity_type: {entity_type}',
                    'output': 0
                }

        except Exception as e:
            print(f'Error in find_in_cache: {str(e)}')
            return {
                'success': False,
                'action': action,
                'error': f'Error in find_in_cache: {str(e)}',
                'output': 0
            }




    def _price_to_str(self, val: Any) -> str:
        """Convert price (dict, number, or string) to display string."""
        if val is None:
            return ''
        if isinstance(val, (int, float)):
            return f"${val:.2f}"
        if isinstance(val, dict):
            amt = val.get('amount') or val.get('value')
            cur = val.get('currency') or 'USD'
            return f"{cur} {amt}" if amt is not None else ''
        return str(val)

    def _get_workspace_and_working_memory(self) -> tuple[bool, str, Dict[str, Any]]:
        """
        Fetch workspace and extract working_memory from intent.
        Returns (success, error_message, working_memory).
        """
        portfolio = self._get_context().portfolio
        org = self._get_context().org
        entity_type = self._get_context().entity_type
        entity_id = self._get_context().entity_id
        thread = self._get_context().thread

        if entity_type != 'org-trip':
            return False, f"Unsupported entity_type: {entity_type}", {}

        response = self.CHC.list_workspaces(portfolio, org, entity_type, entity_id, thread)
        items = response.get('items') or []
        if not items:
            return False, "No workspaces found for this thread", {}

        workspace = items[-1]
        intent = workspace.get('intent') or {}
        wm = intent.get('working_memory') or {}
        return True, "", wm

    def _flatten_hotel_quotes(self, wm: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten hotel_quotes_by_stay into a single list of options (like reducer)."""
        by_stay = wm.get('hotel_quotes_by_stay') or []
        if by_stay:
            flat: List[Dict[str, Any]] = []
            for stay in by_stay:
                if not stay:
                    continue
                if isinstance(stay, list):
                    for room in stay:
                        if isinstance(room, dict):
                            flat.append(room)
                        elif isinstance(room, list):
                            flat.extend(r for r in room if isinstance(r, dict))
                elif isinstance(stay, dict):
                    flat.append(stay)
            return flat
        return wm.get('hotel_quotes') or []

    def _resolve_flight_option(
        self, option_id: str, wm: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Find flight option by option_id in flight_quotes or flight_quotes_by_segment."""
        by_seg = wm.get('flight_quotes_by_segment') or []
        flat = wm.get('flight_quotes') or []
        if by_seg:
            for opts in by_seg:
                if isinstance(opts, list):
                    for o in opts or []:
                        if isinstance(o, dict) and o.get('option_id') == option_id:
                            return o
                elif isinstance(opts, dict) and opts.get('option_id') == option_id:
                    return opts
        for o in flat:
            if isinstance(o, dict) and o.get('option_id') == option_id:
                return o
        return None

    def _resolve_hotel_option(
        self, option_id: str, wm: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Find hotel option by option_id in hotel_quotes or hotel_quotes_by_stay."""
        flat = self._flatten_hotel_quotes(wm)
        for o in flat:
            if isinstance(o, dict) and o.get('option_id') == option_id:
                return o
        return None

    def _flight_option_to_segment(self, opt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Convert a flight option to trip document flight segment format.
        Option may already be in segment format (has 'flights' key) or have 'segments'.
        """
        if opt.get('flights') and isinstance(opt.get('flights'), list):
            vr = self.validate_flight_segment(opt)
            return vr['output'] if vr.get('success') else None
        segs = opt.get('segments') or []
        if segs and isinstance(segs[0], dict):
            seg = segs[0]
            from_code = seg.get('from') or ''
            to_code = seg.get('to') or ''
            depart = seg.get('depart_at') or ''
            arrive = seg.get('arrive_at') or ''
            airline = seg.get('airline') or ''
            fn = seg.get('flight_number') or ''
            synthetic = {
                'airline_logo': opt.get('airline_logo') or '',
                'carbon_emissions': opt.get('carbon_emissions') or {
                    'difference_percent': '', 'this_flight': '', 'typical_for_this_route': ''
                },
                'departure_token': opt.get('departure_token') or '',
                'flights': [{
                    'airline': airline,
                    'airline_logo': opt.get('airline_logo') or '',
                    'airplane': opt.get('airplane') or '',
                    'arrival_airport': {'id': to_code, 'name': to_code, 'time': arrive},
                    'departure_airport': {'id': from_code, 'name': from_code, 'time': depart},
                    'duration': opt.get('total_duration') or '',
                    'extensions': opt.get('extensions') or [],
                    'flight_number': fn,
                    'legroom': opt.get('legroom') or '',
                    'plane_and_crew_by': airline,
                    'travel_class': opt.get('travel_class') or 'economy',
                }],
                'layovers': opt.get('layovers') or [],
                'price': self._price_to_str(opt.get('price') or opt.get('total_price')),
                'total_duration': opt.get('total_duration') or '',
                'type': opt.get('type') or 'direct',
            }
            vr = self.validate_flight_segment(synthetic)
            return vr['output'] if vr.get('success') else None
        return None

    def _hotel_option_to_trip_hotel(self, opt: Dict[str, Any]) -> Dict[str, Any]:
        """Convert hotel option to trip document hotel format."""
        pb = opt.get('total_price') or {}
        amount = pb.get('amount') if isinstance(pb, dict) else opt.get('price')
        return {
            'name': opt.get('hotel_name') or opt.get('name') or '',
            'address': opt.get('address') or '',
            'check_in': opt.get('check_in') or '',
            'check_out': opt.get('check_out') or '',
            'price_per_night': float(amount) if isinstance(amount, (int, float)) else 0,
            'currency': (pb.get('currency') if isinstance(pb, dict) else None) or 'USD',
            'rating': opt.get('star_rating') or opt.get('rating'),
            'property_token': opt.get('property_token') or '',
            'amenities': opt.get('amenities') or [],
        }

    def append_bundle(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve bundle references (flight_option_ids, hotel_option_ids) from
        workspace intent working_memory, then add each resolved flight and hotel
        to the trip document.
        """
        action = "append_bundle"

        try:
            portfolio = self._get_context().portfolio
            org = self._get_context().org
            ring = 'noma_travels'
            entity_type = self._get_context().entity_type
            entity_id = self._get_context().entity_id

            trip_id = None
            if entity_type == 'org-trip':
                parts = entity_id.split('-')
                trip_id = '-'.join(parts[1:])

            if not trip_id:
                raise Exception('No id provided')

            if not isinstance(bundle, dict) or not bundle:
                raise Exception('Bundle must be a non-empty dictionary')

            ok, err, wm = self._get_workspace_and_working_memory()
            if not ok:
                return {
                    'success': False,
                    'action': action,
                    'input': bundle,
                    'output': err,
                    'interface': 'bundles_added'
                }

            flight_ids = bundle.get('flight_option_ids') or (
                [bundle['flight_option_id']] if bundle.get('flight_option_id') else []
            )
            hotel_ids = bundle.get('hotel_option_ids') or (
                [bundle['hotel_option_id']] if bundle.get('hotel_option_id') else []
            )

            added_flights: List[Dict[str, Any]] = []
            added_hotels: List[Dict[str, Any]] = []
            missing: List[str] = []

            for fid in flight_ids:
                if not fid:
                    continue
                opt = self._resolve_flight_option(fid, wm)
                if not opt:
                    missing.append(f"flight:{fid}")
                    continue
                seg = self._flight_option_to_segment(opt)
                if seg:
                    added_flights.append(seg)

            for hid in hotel_ids:
                if not hid:
                    continue
                opt = self._resolve_hotel_option(hid, wm)
                if not opt:
                    missing.append(f"hotel:{hid}")
                    continue
                added_hotels.append(self._hotel_option_to_trip_hotel(opt))

            if missing:
                return {
                    'success': False,
                    'action': action,
                    'input': bundle,
                    'output': f"Could not resolve options in working_memory: {', '.join(missing)}",
                    'interface': 'bundles_added'
                }

            trip_doc = self.DAC.get_a_b_c(portfolio, org, ring, trip_id)
            if not isinstance(trip_doc, dict):
                trip_doc = {}

            existing_flights = list(trip_doc.get('flights') or [])
            existing_hotels = list(trip_doc.get('hotels') or [])

            new_flights = existing_flights + added_flights
            new_hotels = existing_hotels + added_hotels

            input_obj = {'flights': new_flights, 'hotels': new_hotels}
            response_2, _ = self.DAC.put_a_b_c(portfolio, org, ring, trip_id, input_obj)

            if not response_2.get('success'):
                return {
                    'success': False,
                    'action': action,
                    'input': bundle,
                    'output': input_obj,
                    'interface': 'bundles_added'
                }

            summary = {
                'bundle_id': bundle.get('bundle_id'),
                'flights_added': len(added_flights),
                'hotels_added': len(added_hotels),
                'estimated_total': bundle.get('estimated_total'),
                'why_this_bundle': bundle.get('why_this_bundle'),
            }

            return {
                'success': True,
                'action': action,
                'input': bundle,
                'output': summary,
                'interface': 'bundles_added'
            }

        except Exception as e:
            return {
                'success': False,
                'action': action,
                'input': bundle if isinstance(bundle, dict) else {},
                'output': f"Error in append_bundle: {str(e)}",
                'interface': 'bundles_added'
            }




    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run add_bundle
        """

        # Initialize a new request context
        context = RequestContext()
        self._set_context(context)

        leg = payload.get('leg', '')
        if leg == 'return':
            leg = 1
        elif leg == '' or leg is None:
            leg = 0
        else:
            try:
                leg = int(leg)
            except (ValueError, TypeError):
                leg = 0


        # Update context with query parameters
        self._update_context(
            portfolio=payload.get('_portfolio', ''),
            org=payload.get('_org', ''),
            entity_type = payload.get('_entity_type', ''),
            entity_id = payload.get('_entity_id', ''),
            thread = payload.get('_thread', ''),
            leg = leg
        )

        results = []

        # response_1 = self.find_in_cache(payload)
        # results.append(response_1)
        # if not response_1['success']:
        #     return {'success': False, 'output': results}

        response_1 = self.find_in_cache(payload)
        results.append(response_1)

        # Continue with append
        response_2 = self.append_bundle(response_1['output'])
        results.append(response_2)
        canonical = results[-1]['output']

        if not response_2['success']:
            return {'success': False, 'input': payload, 'output': canonical, 'stack': results}

        return {
            'success': True,
            'interface': response_2.get('interface', 'bundles_added'),
            'input': payload,
            'output': canonical,
            'stack': results
        }

    

# Test block
if __name__ == '__main__':
    # Creating an instance
    pass

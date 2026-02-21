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
from typing import Dict, Any, Union, List
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
                workspace = workspaces_list[0]
                if 'cache' not in workspace:
                    print('No cache found in workspace')
                    return {
                        'success': False,
                        'action': action,
                        'error': 'No cache found in workspace',
                        'output': 0
                    }

                cache_key = 'irn:tool_rs:noma/generate_bundles'
                if cache_key not in workspace['cache']:
                    print(f'Cache key {cache_key} not found')
                    return {
                        'success': False,
                        'action': action,
                        'error': f'Cache key {cache_key} not found in workspace',
                        'output': 0
                    }

                cache = workspace['cache'][cache_key]['output']['working_memory']['ranked_bundles']

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

                # Define the JSON schema separately to avoid f-string conflicts
                json_schema = '''{
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "airline_logo": {
                            "type": "string",
                            "format": "uri"
                        },
                        "carbon_emissions": {
                            "type": "object",
                            "properties": {
                                "difference_percent": { "type": "string" },
                                "this_flight": { "type": "string" },
                                "typical_for_this_route": { "type": "string" }
                            },
                            "required": ["difference_percent", "this_flight", "typical_for_this_route"]
                        },
                        "departure_token": {
                            "type": "string"
                        },
                        "flights": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "airline": { "type": "string" },
                                    "airline_logo": { "type": "string", "format": "uri" },
                                    "airplane": { "type": "string" },
                                    "arrival_airport": {
                                        "type": "object",
                                        "properties": {
                                            "id": { "type": "string" },
                                            "name": { "type": "string" },
                                            "time": { "type": "string", "format": "date-time" }
                                        },
                                        "required": ["id", "name", "time"]
                                    },
                                    "departure_airport": {
                                        "type": "object",
                                        "properties": {
                                            "id": { "type": "string" },
                                            "name": { "type": "string" },
                                            "time": { "type": "string", "format": "date-time" }
                                        },
                                        "required": ["id", "name", "time"]
                                    },
                                    "duration": { "type": "string" },
                                    "extensions": {
                                        "type": "array",
                                        "items": { "type": "string" }
                                    },
                                    "flight_number": { "type": "string" },
                                    "legroom": { "type": "string" },
                                    "plane_and_crew_by": { "type": "string" },
                                    "travel_class": { "type": "string" },
                                    "often_delayed_by_over_30_min": { "type": "boolean" },
                                    "overnight": { "type": "boolean" }
                                },
                                "required": [
                                    "airline",
                                    "airline_logo",
                                    "airplane",
                                    "arrival_airport",
                                    "departure_airport",
                                    "duration",
                                    "extensions",
                                    "flight_number",
                                    "legroom",
                                    "plane_and_crew_by",
                                    "travel_class"
                                ],
                                "additionalProperties": false
                            }
                        },
                        "layovers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "duration": { "type": "string" },
                                    "id": { "type": "string" },
                                    "name": { "type": "string" }
                                },
                                "required": ["duration", "id", "name"]
                            }
                        },
                        "price": {
                            "type": "string"
                        },
                        "total_duration": {
                            "type": "string"
                        },
                        "type": {
                            "type": "string"
                        }
                    },
                    "required": [
                        "airline_logo",
                        "carbon_emissions",
                        "departure_token",
                        "flights",
                        "layovers",
                        "price",
                        "total_duration",
                        "type"
                    ],
                    "additionalProperties": false
                }'''

                # Create prompt and call LLM
                prompt_text = f"""
                - You are a very smart assistant that helps user find a json object inside of a cache.
                - The cache is an array that has as many objects as flights options.
                - Given this hint: {hint}, you need to infer to what of the options the user is referring to.

                This is the cache (array):
                {serialized_cache}

                ## Example 1: if the hint is similar to 'I want the first flight', the right option is the first item in the array.
                ## Example 2: If the hint is similar to 'The flight that departs at 6:20' you need to look for the object in flights[].departure_airport.time
                ## Example 3: If the hint is similar to 'The flight that arrives at 9:30' you need to look for the object in flights[].arrival_airport.time
                ## Example 4: If the hint is similar to 'The flight with the least carbon emissions' you need to look for carbon_emissions.this_flight on each object in the array to make a decision.
                ## Example 5: If the hint is similar to 'The cheapest flight' you need to look for the price in the price attribute in each object to make a decision.
                ## Example 6: If the hint is similar to 'The shortest trip' you need to look for the duration in the total_duration attribute in each object to make a decision.
                ## Example 7: If the hint is similar to 'The airline that is called Aeromexico' you need to look for the object in flights[].airline.

                - Please notice that in certain results, you'll find that the flights array has more than one object. That's because the passenger needs to get into a second or third flight to eventually make it to their destination.

                - The JSON Schema for each Flight object in the array is shown below:

                {json_schema}

                All you need to output is the index number in the array that indicates what object is the one that the hint refers to.
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
                selected_flight = self.sanitize(cache[selected_index])
                return {'success':True,'action':action,'input': payload,'output':selected_flight}

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




    def append_bundle(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        """
        
        TO BE IMPLEMENTED
        You need to iterate through the bundle and add each one of its segments to the trip document
        
        """
        action = "append_bundle"

        try:
            portfolio = self._get_context().portfolio
            org = self._get_context().org
            ring = 'noma_travels'

            entity_type = self._get_context().entity_type
            entity_id = self._get_context().entity_id
            leg = int(self._get_context().leg)

            # The trip_id sent through the payload is ignored as it could be spoofed
            # Instead we obtain the trip id from the entity_id.
            trip_id = None
            if entity_type == 'org-trip':
                #'a066a651f062-f37acf92-e2da-45c2-bdd9-41afa66d81e2'
                #'f37acf92-e2da-45c2-bdd9-41afa66d81e2'
                parts = entity_id.split('-')
                trip_id = '-'.join(parts[1:])



            print('trip_id >>',trip_id)

            if trip_id:

                #1. We get the trip document we are going to modify
                response_1 = self.DAC.get_a_b_c(portfolio,org,ring,trip_id)


                print(f'add_flight > Segment to be inserted:')
                print(segment)
                print(f'type: {type(segment).__name__}')
                if not isinstance(segment, dict):
                    raise Exception('Segment must be a dictionary')

                if segment:
                    # We validate the segment.
                    validation_result = self.validate_flight_segment(segment)

                    #We modify the document by adding the flights.
                    if validation_result["success"]:

                        # Create a new list based on the old flights list
                        new_list = response_1['flights'].copy()

                        # Insert the validation result at the specified index position
                        # If the index is beyond the current list length, extend the list
                        if leg >= len(new_list):
                            # Extend the list with empty dictionaries if needed
                            new_list.extend([{}] * (leg - len(new_list) + 1))

                        # Insert the validation result at the specified position
                        new_list[leg] = validation_result["output"]

                        # Create input object with the complete updated flights list
                        input_obj = {'flights': new_list}

                        print(f'add_flight > Segment to be inserted > AFTER VALIDATION:')
                        print(f'Inserted at position {leg}:')
                        print(validation_result["output"])
                        print(f'Complete new flights list:')
                        print(new_list)
                        print(f"TYPE:{type(input_obj).__name__}")



                        #3. We update the document in the backend
                        response_2, st = self.DAC.put_a_b_c(portfolio,org,ring,trip_id,input_obj)


                        if not response_2['success']:
                            return {
                                'success': False,
                                'action': action,
                                'input': segment,
                                'output': input_obj
                            }
                        else:
                            #Success scenario.
                            #Interface:reload asks for a reload as the trip doc has changed.
                            return {
                                'success': True,
                                'action': action,
                                'input': segment,
                                'output': input_obj,
                                'interface':'reload'
                            }
                    else:
                        return{
                            'success': False,
                            'action': action,
                            'input': segment,
                            'output': validation_result["message"]
                        }


                raise Exception('Segment is empty')


            raise Exception('No id provided')


        except Exception as e:
            return {
                'success': False,
                'action': action,
                'input': '',
                'output': f"Error in replace_flight: {str(e)}"
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

            return {'success': False, 'input':payload, 'output': canonical, 'stack': results}


        # All went well, report back
        return {'success': True, 'interface': 'add_flight', 'input': payload, 'output':canonical,'stack': results}

    

# Test block
if __name__ == '__main__':
    # Creating an instance
    pass

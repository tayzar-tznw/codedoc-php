<?php
declare(strict_types=1);

namespace Shared\Contracts;

interface Clock
{
    public function now(): int;
}
